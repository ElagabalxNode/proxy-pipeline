"""Separate REALITY identity for the RU -> WORLD chain: fresh setup, explicit migration, rollback."""

import base64
import copy
import hashlib
import json
import os

import pytest
import yaml
from typer.testing import CliRunner

from pipelinectl import cli
from test_bridge_apply import apply_env as apply_env_fixture
from test_bridge_apply import run_apply
from test_configure import configure_env as configure_env_fixture
from test_world_transaction import FakePanel, FakeRemote, world
from test_world_transaction import identity as identity_fixture
from test_world_transaction import options as options_fixture
from test_world_transaction import populated as populated_fixture

apply_env = apply_env_fixture
configure_env = configure_env_fixture
identity = identity_fixture
options = options_fixture
populated = populated_fixture

TARGET = {"separate_chain": True, "chain_reality_sni": "cdn.example.ru",
          "chain_reality_dest": "cdn.example.ru:443"}


def encode(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class KeyRemote(FakeRemote):
    """Fresh requests get distinct keys; a derived public key depends only on the private key."""

    def __init__(self):
        super().__init__()
        self.generated = 0

    def keys(self, identity):
        private = identity.get("private_key")
        if not private:
            self.generated += 1
            private = encode(os.urandom(32))
        public = encode(hashlib.sha256(private.encode()).digest())
        if identity.get("public_key", public) != public:
            raise world.WorldError("public key does not belong to the private key")
        return {"private_key": private, "public_key": public}


def reality(row):
    return json.loads(row["streamSettings"])["realitySettings"]


def by_remark(panel):
    return {row["remark"]: row for row in panel.rows}


@pytest.fixture
def shared(identity, tmp_path):
    """A WORLD configured before the split: chain and direct share one REALITY identity."""
    remote = KeyRemote()
    identity["public_key"] = remote.keys({"private_key": identity["private_key"]})["public_key"]
    panel = FakePanel(world.new_payloads(identity))
    (tmp_path / "runtime.json").write_text(json.dumps(identity) + "\n")
    return panel, remote


def test_existing_shared_identity_is_kept_until_migration_is_requested(options, shared, tmp_path):
    panel, remote = shared
    before = panel.list()
    # A probed chain target alone is not a request to change a serving WORLD.
    result = world.transact({**options, **TARGET, "separate_chain": False}, tmp_path, panel, remote)
    assert result["inbounds_changed"] == 0 and panel.rows == before and remote.generated == 0
    runtime = json.loads((tmp_path / "runtime.json").read_text())
    assert runtime["chain_private_key"] == runtime["private_key"]
    assert runtime["chain_public_key"] == runtime["public_key"]
    assert runtime["chain_reality_sni"] == runtime["reality_sni"] == "existing.example"


def test_migration_changes_only_the_chain_inbound_and_is_stable(options, shared, tmp_path):
    panel, remote = shared
    direct_before = copy.deepcopy(by_remark(panel)["VLESS-Reality-Vision"])
    ss_before = copy.deepcopy(by_remark(panel)["SS2022-Fallback"])
    chain_id = by_remark(panel)["Chain-Bridge-Entry"]["id"]

    result = world.transact({**options, **TARGET}, tmp_path, panel, remote)
    assert result["inbounds_changed"] == 1 and panel.calls == [("update", chain_id)]
    rows = by_remark(panel)
    assert rows["VLESS-Reality-Vision"] == direct_before  # direct client links are untouched
    assert rows["SS2022-Fallback"] == ss_before
    chain, runtime = reality(rows["Chain-Bridge-Entry"]), json.loads((tmp_path / "runtime.json").read_text())
    assert chain["privateKey"] == runtime["chain_private_key"] != runtime["private_key"]
    assert chain["shortIds"] == [runtime["chain_short_id"]] != [runtime["short_id"]]
    assert chain["serverNames"] == ["cdn.example.ru"] and chain["dest"] == "cdn.example.ru:443"
    assert runtime["chain_public_key"] == remote.keys({"private_key": chain["privateKey"]})["public_key"]
    assert runtime["chain_public_key"] != runtime["public_key"]
    assert runtime["reality_sni"] == "existing.example" and runtime["uuid"] != runtime["chain_uuid"]
    # The full candidate validated by WORLD's Xray already carried the new chain identity.
    validated = next(ib for ib in remote.candidates[0]["inbounds"] if ib["port"] == options["chain_port"])
    assert validated["streamSettings"]["realitySettings"]["privateKey"] == chain["privateKey"]

    # Repeats (with or without the flag) keep the migrated keys: no writes, no new key material.
    saved, generated = (tmp_path / "runtime.json").read_bytes(), remote.generated
    panel.calls.clear()
    for repeat in ({**options, **TARGET}, options):
        again = world.transact(repeat, tmp_path, panel, remote)
        assert again == {"changed": False, "inbounds_changed": 0, "identity_source": "live"}
    assert panel.calls == [] and remote.generated == generated
    assert (tmp_path / "runtime.json").read_bytes() == saved

    # A newly measured target replaces only SNI/dest; the bridge keeps the same chain key.
    retarget = {**options, **TARGET, "chain_reality_sni": "edge.example.ru",
                "chain_reality_dest": "edge.example.ru:443"}
    world.transact(retarget, tmp_path, panel, remote)
    moved = reality(by_remark(panel)["Chain-Bridge-Entry"])
    assert moved["serverNames"] == ["edge.example.ru"] and moved["privateKey"] == chain["privateKey"]
    assert remote.generated == generated


@pytest.mark.parametrize("after_commit", [False, True])
def test_failed_migration_restores_the_shared_chain_inbound(options, shared, tmp_path, after_commit):
    panel, remote = shared
    before, saved = panel.list(), (tmp_path / "runtime.json").read_bytes()
    panel.fail_at, panel.after_commit = 1, after_commit
    with pytest.raises(world.WorldError, match="runtime restored"):
        world.transact({**options, **TARGET}, tmp_path, panel, remote)
    assert panel.rows == before
    assert (tmp_path / "runtime.json").read_bytes() == saved
    assert not (tmp_path / ".world-transaction.json").exists()


@pytest.mark.parametrize("target", [
    {}, {"chain_reality_sni": "cdn.example.ru"},
    {"chain_reality_sni": "cdn example", "chain_reality_dest": "cdn.example.ru:443"},
])
def test_migration_without_a_probed_target_changes_nothing(options, shared, tmp_path, target):
    panel, remote = shared
    with pytest.raises(world.WorldError, match="probed chain REALITY target"):
        world.transact({**options, "separate_chain": True, **target}, tmp_path, panel, remote)
    assert panel.calls == [] and remote.generated == 0
    assert not (tmp_path / ".world-transaction.json").exists()


@pytest.mark.parametrize("migrate", [False, True])
def test_new_world_never_shares_reality_keys_with_bridges(options, tmp_path, migrate):
    panel, remote = FakePanel(), KeyRemote()
    world.transact({**options, **(TARGET if migrate else {})}, tmp_path, panel, remote)
    rows, runtime = by_remark(panel), json.loads((tmp_path / "runtime.json").read_text())
    direct, chain = reality(rows["VLESS-Reality-Vision"]), reality(rows["Chain-Bridge-Entry"])
    assert direct["privateKey"] != chain["privateKey"] and direct["shortIds"] != chain["shortIds"]
    assert chain["privateKey"] == runtime["chain_private_key"] and remote.generated == 2
    assert direct["serverNames"] == ["new-target.example"]
    assert chain["serverNames"] == ["cdn.example.ru" if migrate else "new-target.example"]
    # Empty panel + this runtime (a rebuilt panel on the same host) restores the same chain key.
    restored = FakePanel()
    result = world.transact(options, tmp_path, restored, remote)
    assert result["identity_source"] == "runtime" and remote.generated == 2
    assert reality(by_remark(restored)["Chain-Bridge-Entry"]) == chain


def test_partial_chain_identity_in_runtime_is_refused(options, shared, tmp_path):
    panel, remote = shared
    runtime = json.loads((tmp_path / "runtime.json").read_text())
    runtime["chain_private_key"] = encode(b"\x07" * 32)
    (tmp_path / "runtime.json").write_text(json.dumps(runtime))
    with pytest.raises(world.WorldError, match="invalid WORLD identity/runtime"):
        world.transact(options, tmp_path, panel, remote)
    assert panel.calls == []


def test_bridge_outbound_uses_the_chain_identity_not_the_direct_one(apply_env):
    root, _, _ = apply_env
    play = yaml.safe_load((root / "play.yml").read_text())
    state = play[0]["vars"]["exit_state"]

    def outbound():
        result = run_apply(root)
        assert result.returncode == 0, result.stdout + result.stderr
        config = json.loads((root / "config.json").read_text())
        return next(o for o in config["outbounds"] if o["tag"] == "chain-to-exit")["streamSettings"][
            "realitySettings"]

    legacy = outbound()  # runtime written before the split: the chain still uses direct parameters
    assert (legacy["publicKey"], legacy["shortId"], legacy["serverName"]) == (
        state["public_key"], state["short_id"], state["reality_sni"])
    state.update(chain_public_key=encode(b"\x09" * 32), chain_short_id="99887766",
                 chain_reality_sni="cdn.example.ru")
    (root / "play.yml").write_text(yaml.safe_dump(play))
    split = outbound()
    assert (split["publicKey"], split["shortId"], split["serverName"]) == (
        encode(b"\x09" * 32), "99887766", "cdn.example.ru")
    assert state["public_key"] not in json.dumps(split)


def test_cli_requires_a_chain_target_probed_from_this_exit(configure_env, monkeypatch):
    cfg, _, _, inventory, playbook = configure_env
    state = {"chain": None}

    def load(st, purpose="direct"):
        if purpose == "direct":
            return {"selected": {"sni": "test.example", "dest": "test.example:443"}}
        if state["chain"] is None:
            raise FileNotFoundError("chain_reality_target.json not found — run probe-targets")
        return {"selected": state["chain"]}
    monkeypatch.setattr(cli.probe, "load_reality_target", load)
    runner = CliRunner()
    missing = runner.invoke(cli.app, ["configure", "--separate-chain-identity"])
    assert missing.exit_code != 0 and "probe-targets" in missing.output
    state["chain"] = {"sni": "cdn.example.ru", "dest": "cdn.example.ru:443", "exit_ip": "192.0.2.77"}
    stale = runner.invoke(cli.app, ["configure", "--separate-chain-identity"])
    assert stale.exit_code != 0 and "192.0.2.77" in stale.output
    for combo in ("--bridge-only", "--exit-firewall-only"):
        assert runner.invoke(cli.app, ["configure", "--separate-chain-identity", combo]).exit_code != 0
    inventory.assert_not_called()
    playbook.assert_not_called()

    state["chain"]["exit_ip"] = "203.0.113.1"
    done = runner.invoke(cli.app, ["configure", "--separate-chain-identity"])
    assert done.exit_code == 0, done.output
    extra = playbook.call_args.args[3]
    assert extra["world_separate_chain"] is True
    assert (extra["chain_reality_sni"], extra["chain_reality_dest"]) == ("cdn.example.ru", "cdn.example.ru:443")
    assert extra["reality_sni"] == "test.example"  # the direct target is a separate measurement
    assert "interrupted" in done.output
    plain = runner.invoke(cli.app, ["configure"])
    assert plain.exit_code == 0 and "world_separate_chain" not in playbook.call_args.args[3]
