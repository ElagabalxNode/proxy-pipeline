"""Managed WORLD identity and compensating API transaction, with fault injection."""

import copy
import importlib.util
import json
import os
import sqlite3
import socket
import sys
from pathlib import Path

import pytest

from test_bridge_support import PRIVATE, PUBLIC, support
from test_bridge_config import free_port, running_xray

sys.modules.setdefault("bridge_support", support)
FILES = Path(__file__).resolve().parents[2] / "ansible/roles/exit_xui_reality/files"


def load(name):
    spec = importlib.util.spec_from_file_location(name, FILES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


world = load("world_support")
world_remote = load("world_remote")


@pytest.fixture
def options():
    return {"exit_ip": "192.0.2.1", "panel_port": 2053, "panel_path": "/test",
            "panel_user": "admin", "panel_pass": "test-secret",
            "reality_sni": "new-target.example", "reality_dest": "new-target.example:443",
            "reality_port": 443, "ss_port": 8388, "chain_port": 8443,
            "ss_method": "2022-blake3-aes-128-gcm"}


@pytest.fixture
def identity(options):
    return {**options, "uuid": "e11fd630-493a-4181-b514-8e462ce601e5",
            "chain_uuid": "6aac169a-291c-49d8-85fc-cf3c4e22e7f2", "private_key": PRIVATE,
            "public_key": PUBLIC, "short_id": "aabbccdd", "chain_path": "/existing-chain",
            "reality_sni": "existing.example", "reality_dest": "existing.example:443",
            "ss_password": "AQEBAQEBAQEBAQEBAQEBAQ=="}


class FakePanel:
    def __init__(self, rows=()):
        self.rows = copy.deepcopy(list(rows))
        for n, row in enumerate(self.rows, 1):
            row.setdefault("id", n)
        self.calls = []
        self.fail_at = None
        self.after_commit = False
        self.next_id = 100
        self.config_edit = lambda config: config

    def list(self):
        return copy.deepcopy(self.rows)

    def config(self):
        config = {"inbounds": [], "outbounds": [{"tag": "direct", "protocol": "freedom"}],
                  "log": {"loglevel": "warning"},
                  "routing": {"domainStrategy": "AsIs", "rules": []}}
        for row in self.rows:
            ib = {k: row[k] for k in ("port", "protocol", "tag")}
            ib["listen"] = row["listen"] or "0.0.0.0"
            ib.update({k: world.object_json(row[k]) for k in world.JSON_FIELDS})
            config["inbounds"].append(ib)
        return self.config_edit(config)

    def put(self, row, inbound_id=None):
        self.calls.append(("add" if inbound_id is None else "update", inbound_id))
        fail = self.fail_at == len(self.calls)
        if fail and not self.after_commit:
            raise world.WorldError("simulated API failure")
        row = copy.deepcopy(row)
        if inbound_id is None:
            row["id"] = self.next_id
            self.next_id += 1
            self.rows.append(row)
        else:
            old = next(i for i in self.rows if i["id"] == inbound_id)
            self.rows[self.rows.index(old)] = {**old, **row, "id": inbound_id}
        if fail:
            raise world.WorldError("simulated lost response after commit")

    def delete(self, inbound_id):
        self.calls.append(("delete", inbound_id))
        self.rows = [row for row in self.rows if row["id"] != inbound_id]

    def healthy(self):
        return True


class FakeRemote:
    def __init__(self):
        self.candidates = []
        self.verified = []
        self.bad_candidate = False
        self.bad_start_once = False

    def keys(self, identity):
        return {"private_key": identity.get("private_key", PRIVATE),
                "public_key": identity.get("public_key", PUBLIC)}

    def validate(self, config):
        self.candidates.append(copy.deepcopy(config))
        if self.bad_candidate:
            raise world.WorldError("candidate rejected")
        return {"version": "test-fixture", "sha256": "a" * 64}

    def verify(self, rows, panel):
        self.verified.append(copy.deepcopy(rows))
        if self.bad_start_once:
            self.bad_start_once = False
            raise world.WorldError("simulated startup failure")


@pytest.fixture
def populated(identity, tmp_path):
    rows = world.new_payloads(identity)
    foreign = copy.deepcopy(rows[1])
    foreign.update(remark="unmanaged", port=9443, tag="foreign", up=912)
    panel = FakePanel([*rows, foreign])
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps(identity) + "\n")
    runtime.chmod(0o600)
    return panel, FakeRemote(), runtime


def test_repeat_preserves_live_identity_and_makes_no_inbound_writes(options, populated, tmp_path):
    panel, remote, runtime = populated
    original = panel.list()
    first = world.transact(options, tmp_path, panel, remote)
    saved = runtime.read_bytes()
    second = world.transact(options, tmp_path, panel, remote)
    assert first["identity_source"] == "live"
    assert not second["changed"]
    assert panel.rows == original
    assert panel.calls == []
    assert runtime.read_bytes() == saved
    data = json.loads(saved)
    assert data["reality_sni"] == "existing.example"  # a new probe must not rotate client SNI
    assert data["chain_path"] == "/existing-chain"
    assert remote.candidates[-1] == panel.config()
    assert not (tmp_path / ".world-transaction.json").exists()


def test_fresh_setup_and_same_host_runtime_recovery(options, identity, tmp_path):
    panel, remote = FakePanel(), FakeRemote()
    result = world.transact(options, tmp_path, panel, remote)
    assert result["identity_source"] == "fresh"
    data = json.loads((tmp_path / "runtime.json").read_text())
    assert data["uuid"] != data["chain_uuid"]
    assert len(panel.rows) == 3
    assert (tmp_path / "runtime.json").stat().st_mode & 0o777 == 0o600
    (tmp_path / "runtime.json").write_text(json.dumps(identity))
    restored = FakePanel()
    result = world.transact(options, tmp_path, restored, remote)
    assert result["identity_source"] == "runtime"
    assert world.live_identity(restored.rows, options["exit_ip"], remote.keys)["uuid"] == identity["uuid"]


def test_backup_from_another_exit_is_not_cloned(options, identity, tmp_path):
    (tmp_path / "runtime.json").write_text(json.dumps({**identity, "exit_ip": "192.0.2.99"}))
    world.transact(options, tmp_path, FakePanel(), FakeRemote())
    assert json.loads((tmp_path / "runtime.json").read_text())["uuid"] != identity["uuid"]


@pytest.mark.parametrize("after_commit", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_mid_apply_failure_restores_managed_inbounds_and_exact_state(
    options, populated, tmp_path, existing, after_commit,
):
    panel, remote, runtime = populated
    if not existing:
        panel.rows = panel.rows[-1:]  # preserve the foreign inbound in both paths
        runtime.unlink()
    saved = world.snapshot(runtime)
    before = panel.list()
    panel.fail_at, panel.after_commit = 2, after_commit
    requested = {**options, "reality_port": 1443, "ss_port": 18388, "chain_port": 18443}
    with pytest.raises(world.WorldError, match="runtime restored"):
        world.transact(requested, tmp_path, panel, remote)
    assert panel.rows == before
    assert world.snapshot(runtime) == saved
    assert not (tmp_path / ".world-transaction.json").exists()
    if existing:
        assert all(action != "delete" for action, _ in panel.calls)


@pytest.mark.parametrize("failure", ["validation", "startup", "state_write"])
def test_local_failure_never_publishes_unverified_runtime(options, populated, tmp_path, monkeypatch, failure):
    panel, remote, runtime = populated
    saved, before = runtime.read_bytes(), panel.list()
    requested = {**options, "chain_port": 18443}
    remote.bad_candidate = failure == "validation"
    remote.bad_start_once = failure == "startup"
    write = world.atomic_write
    if failure == "state_write":
        def fail_state(path, content, mode=0o600):
            if path == runtime:
                raise OSError("simulated full controller disk")
            write(path, content, mode)
        monkeypatch.setattr(world, "atomic_write", fail_state)
    with pytest.raises(world.WorldError):
        world.transact(requested, tmp_path, panel, remote)
    assert panel.rows == before
    assert runtime.read_bytes() == saved
    if failure == "validation":
        assert panel.calls == []


@pytest.mark.parametrize("damage", ["partial", "duplicate", "disabled", "multi_client", "ss_key", "state", "foreign_port"])
def test_ambiguous_or_corrupt_inputs_stop_before_api_mutations(options, populated, tmp_path, damage):
    panel, remote, runtime = populated
    if damage == "partial":
        panel.rows.pop(1)
    elif damage == "duplicate":
        panel.rows.append(copy.deepcopy(panel.rows[0]))
    elif damage == "disabled":
        panel.rows[0]["enable"] = False
    elif damage == "multi_client":
        settings = json.loads(panel.rows[0]["settings"])
        settings["clients"].append(settings["clients"][0])
        panel.rows[0]["settings"] = json.dumps(settings)
    elif damage == "ss_key":
        settings = json.loads(panel.rows[1]["settings"])
        settings["password"] = "broken"
        panel.rows[1]["settings"] = json.dumps(settings)
    elif damage == "state":
        runtime.write_text("{")
    else:
        options["chain_port"] = 9443
    with pytest.raises((world.WorldError, ValueError)):
        world.transact(options, tmp_path, panel, remote)
    assert panel.calls == []
    assert not (tmp_path / ".world-transaction.json").exists()


def test_candidate_covers_foreign_inbounds_and_detects_identity_drift(options, populated, tmp_path):
    panel, remote, runtime = populated
    def drift(config):
        config["inbounds"][0]["streamSettings"]["realitySettings"]["privateKey"] = PUBLIC
        return config
    panel.config_edit = drift
    with pytest.raises(world.WorldError, match="identity drift"):
        world.transact(options, tmp_path, panel, remote)
    assert not panel.calls
    assert not remote.candidates


def test_failed_rollback_retains_journal_and_next_run_recovers(options, populated, tmp_path, monkeypatch):
    panel, remote, runtime = populated
    before, saved = panel.list(), runtime.read_bytes()
    remote.bad_start_once = True
    put = panel.put
    def broken_rollback(row, inbound_id=None):
        if row["port"] == options["chain_port"]:
            raise world.WorldError("rollback unavailable")
        put(row, inbound_id)
    monkeypatch.setattr(panel, "put", broken_rollback)
    with pytest.raises(world.WorldError, match="rollback incomplete"):
        world.transact({**options, "chain_port": 18443}, tmp_path, panel, remote)
    journal = tmp_path / ".world-transaction.json"
    assert journal.stat().st_mode & 0o777 == 0o600
    assert runtime.read_bytes() == saved
    monkeypatch.setattr(panel, "put", put)
    with pytest.raises(world.WorldError, match="unfinished WORLD transaction restored"):
        world.transact(options, tmp_path, panel, remote)
    assert panel.rows == before
    assert runtime.read_bytes() == saved
    assert not journal.exists()


def test_recovery_after_controller_interrupt_during_state_publish(options, populated, tmp_path, monkeypatch):
    panel, remote, runtime = populated
    old_rows, old_state = panel.list(), runtime.read_bytes()
    write = world.atomic_write
    def crash_after_publish(path, content, mode=0o600):
        write(path, content, mode)
        if path == runtime:
            raise KeyboardInterrupt("simulated controller termination")
    monkeypatch.setattr(world, "atomic_write", crash_after_publish)
    with pytest.raises(KeyboardInterrupt):
        world.transact({**options, "chain_port": 18443}, tmp_path, panel, remote)
    assert runtime.read_bytes() != old_state
    assert (tmp_path / ".world-transaction.json").exists()
    monkeypatch.setattr(world, "atomic_write", write)
    with pytest.raises(world.WorldError, match="unfinished WORLD transaction restored"):
        world.transact(options, tmp_path, panel, remote)
    assert runtime.read_bytes() == old_state
    assert panel.rows == old_rows


def test_concurrent_controller_edit_is_not_overwritten(options, populated, tmp_path):
    panel, remote, runtime = populated
    def external_edit(rows, _panel):
        runtime.write_text('{"operator": "unrelated new state"}')
    remote.verify = external_edit
    with pytest.raises(world.WorldError, match="rollback incomplete"):
        world.transact({**options, "chain_port": 18443}, tmp_path, panel, remote)
    assert runtime.read_text() == '{"operator": "unrelated new state"}'
    assert (tmp_path / ".world-transaction.json").exists()


@pytest.mark.parametrize("value", ["{", "{}", '{"schema":1,"exit_ip":"192.0.2.99"}'])
def test_invalid_journal_stops_before_mutations(options, populated, tmp_path, value):
    panel, remote, runtime = populated
    (tmp_path / ".world-transaction.json").write_text(value)
    saved = runtime.read_bytes()
    with pytest.raises(world.WorldError, match="invalid recovery journal"):
        world.transact(options, tmp_path, panel, remote)
    assert not panel.calls
    assert runtime.read_bytes() == saved


def test_unexpected_panel_config_normalization_rolls_back(options, populated, tmp_path):
    panel, remote, runtime = populated
    before, saved = panel.list(), runtime.read_bytes()
    def unexpected(config):
        if any(row["port"] == 18443 for row in panel.rows):
            config["routing"]["rules"] = [{"type": "field", "network": "tcp", "outboundTag": "direct"}]
            config["outbounds"][0]["protocol"] = "blackhole"
        return config
    panel.config_edit = unexpected
    with pytest.raises(world.WorldError, match="runtime restored"):
        world.transact({**options, "chain_port": 18443}, tmp_path, panel, remote)
    assert panel.rows == before
    assert runtime.read_bytes() == saved


def test_bootstrap_requires_empty_sqlite_and_creates_consistent_private_backup(tmp_path):
    database = tmp_path / "x-ui.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE inbounds(id INTEGER)")
    request = {"action": "bootstrap", "database": str(database), "binary": sys.executable}
    backup = Path(world_remote.execute(request)["backup"])
    assert backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT count(*) FROM inbounds").fetchone()[0] == 0
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO inbounds VALUES (1)")
    with pytest.raises(ValueError, match="nonempty"):
        world_remote.execute(request)
    request["database"] = str(tmp_path / "missing.db")
    with pytest.raises(sqlite3.OperationalError):
        world_remote.execute(request)


def test_real_world_xray_validates_full_candidate_and_key_recovery(options, identity, tmp_path):
    binary = os.environ.get("XRAY_TEST_BINARY")
    if not binary:
        pytest.skip("set XRAY_TEST_BINARY for WORLD candidate validation")
    fresh = world.new_payloads(identity)
    config = world.full_candidate(FakePanel().config(), {}, fresh)
    result = world_remote.execute({"action": "validate", "binary": binary, "config": config})
    assert "25.10.15" in result["version"]
    keys = world_remote.execute({"action": "keys", "binary": binary, "private_key": PRIVATE})
    adapted = {**identity, "sni": identity["reality_sni"], "dest": identity["reality_dest"]}
    adapted.pop("public_key")  # derive the actual public key, rather than the test fixture's
    assert support.recover_keys(keys["output"], adapted)["private_key"] == PRIVATE
    config["inbounds"][0]["protocol"] = "invalid-protocol"
    with pytest.raises(ValueError, match="rejected the full candidate"):
        world_remote.execute({"action": "validate", "binary": binary, "config": config})


def test_world_checks_listener_owner_with_real_xray(identity, tmp_path):
    binary = os.environ.get("XRAY_TEST_BINARY")
    if not binary:
        pytest.skip("set XRAY_TEST_BINARY for listener ownership regression")
    binary = Path(binary).resolve()
    config = world.full_candidate(FakePanel().config(), {}, world.new_payloads(identity))
    ports = []
    for inbound in config["inbounds"]:
        port = free_port()
        ports.append(port)
        inbound.update(listen="127.0.0.1", port=port)
    with running_xray(binary, config, tmp_path / "world.json", ports[0]):
        assert world_remote.execute({"action": "ports", "binary": str(binary), "ports": ports})["ok"]
        with socket.socket() as other:
            other.bind(("127.0.0.1", 0))
            other.listen()
            with pytest.raises(ValueError, match="not owned"):
                world_remote.execute({"action": "ports", "binary": str(binary),
                                      "ports": [other.getsockname()[1]]})
