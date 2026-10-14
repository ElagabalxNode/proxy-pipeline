from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pipelinectl import bridge_rotate, cli, vdsina


class FakeVdsina:
    """The slice of the VDSina API a rotation uses; servers come up on the next status read."""

    def __init__(self):
        self.servers = {1: {"id": 1, "status": "active", "ip": {"id": 10, "ip": "198.51.100.1"},
                            "datacenter": {"id": 3}, "server-plan": {"id": 136}, "template": {"id": 36}}}
        self.extra = [{"id": 7, "ip": [{"id": 11, "ip": "198.51.100.2"}], "server": {"id": 1}}]
        self.keys = {5: "ssh-ed25519 AAAAexisting other@host"}
        self.created, self.deleted, self.addresses = [], [], iter(["203.0.113.1", "203.0.113.2", "203.0.113.3"])

    def __call__(self, method, path, body=None):
        if (method, path) == ("GET", "/server"):
            return list(self.servers.values())
        if (method, path) == ("GET", "/server-ip"):
            return self.extra
        if (method, path) == ("GET", "/ssh-key"):
            return [{"id": k, "name": "k"} for k in self.keys]
        if method == "GET" and path.startswith("/ssh-key/"):
            return {"data": self.keys[int(path.rsplit("/", 1)[1])]}
        if (method, path) == ("POST", "/ssh-key"):
            self.keys[6] = body["data"]
            return {"id": 6}
        if (method, path) == ("POST", "/server"):
            sid = 100 + len(self.created)
            self.created.append(body)
            self.servers[sid] = {"id": sid, "status": "new", "ip": None}
            return {"id": sid}
        if method == "GET" and path.startswith("/server/"):
            server = self.servers[int(path.rsplit("/", 1)[1])]
            if server["status"] == "new":
                server.update(status="active", ip={"ip": next(self.addresses)})
            return server
        if method == "DELETE" and path.startswith("/server/"):
            self.deleted.append(int(path.rsplit("/", 1)[1]))
            return None
        raise AssertionError(f"unexpected {method} {path}")


def test_the_bridge_is_found_by_its_main_or_additional_address_and_copied():
    api = FakeVdsina()
    assert vdsina.find_server(api, "198.51.100.1")["id"] == 1
    assert vdsina.find_server(api, "198.51.100.2")["id"] == 1  # the entry on an extra address
    assert vdsina.find_server(api, "192.0.2.9") is None
    assert vdsina.spec_of(api, 1) == {"datacenter": 3, "server-plan": 136, "template": 36}


def test_the_controller_key_is_registered_once():
    api = FakeVdsina()
    assert vdsina.ensure_ssh_key(api, "ssh-ed25519 AAAAexisting me@controller\n") == 5
    assert vdsina.ensure_ssh_key(api, "ssh-ed25519 AAAAnew me@controller") == 6
    assert vdsina.ensure_ssh_key(api, "ssh-ed25519 AAAAnew again") == 6


def _search(api, verdicts, attempts=3):
    log = []
    found = bridge_rotate.find_good_server(
        api, {"datacenter": 3, "server-plan": 136, "template": 36}, 6,
        check=lambda ip: (verdicts.pop(0), [f"checked {ip}"]),
        ready=lambda ip: True, attempts=attempts, echo=log.append, name="pp-bridge",
        wait=lambda sid: vdsina.wait_ready(api, sid, sleep=lambda s: None),
    )
    return found, log


def test_rejected_candidates_are_deleted_and_the_first_good_one_is_kept():
    api = FakeVdsina()
    found, log = _search(api, [False, True])
    assert (found.server_id, found.ip, found.passed) == (101, "203.0.113.2", True)
    assert api.deleted == [100]
    # no paid backup schedule, the controller key, the same datacenter/plan/template
    assert api.created[0] == {"datacenter": 3, "server-plan": 136, "template": 36,
                              "name": "pp-bridge-1", "ssh-key": 6, "schedule": "0"}
    assert any("deleting rejected server 100" in line for line in log)


def test_when_every_candidate_fails_nothing_is_left_running():
    api = FakeVdsina()
    found, _ = _search(api, [False, False], attempts=2)
    assert found is None and api.deleted == [100, 101]


def _runner(stdout="", rc=0, record=None):
    def run(cmd, **kwargs):
        if record is not None:
            record.append(cmd[-1] if isinstance(cmd, list) else cmd)
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr="")
    return run


@pytest.mark.parametrize("tls,tcp,ok", [(12, 4, True), (9, 3, True), (0, 4, False), (12, 0, False)])
def test_outbound_gate_rejects_an_address_without_foreign_tls_or_path_to_the_exit(tls, tcp, ok):
    # 2026-10-13: the filtered VDSina address got 0 of 20 TLS handshakes to foreign sites.
    out = json.dumps({"tls": tls, "tls_total": 12, "exit_tcp": tcp, "exit_total": 4})
    got, note = bridge_rotate.outbound_gate("203.0.113.1", "root", Path("k"), "198.51.100.9", 8443, _runner(out))
    assert got is ok and f"{tls}/12" in note


def test_inbound_gate_measures_from_outside_and_always_cleans_up(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("CERT")
    key.write_text("KEY")
    calls: list[str] = []
    ok, note = bridge_rotate.inbound_gate("203.0.113.1", "root", Path("k"), cert, key, "lk.example.ru",
                                          _runner(record=calls), reach=lambda ip, host: (8, 20))
    assert not ok and "8 of 20" in note
    assert "rm -rf /root/.pp-gate" in calls[-1]
    calls.clear()
    ok, note = bridge_rotate.inbound_gate("203.0.113.1", "root", Path("k"), cert, key, "lk.example.ru",
                                          _runner(record=calls), reach=lambda ip, host: (19, 20))
    assert ok and "rm -rf /root/.pp-gate" in calls[-1]

    def unavailable(ip, host):
        raise OSError("globalping down")
    ok, note = bridge_rotate.inbound_gate("203.0.113.1", "root", Path("k"), cert, key, "lk.example.ru",
                                          _runner(), reach=unavailable)
    assert ok and "not measured" in note


def test_a_dirty_address_stops_the_checks_early():
    dirty = {"ports": [22, 25, 110, 3389], "tags": ["malware"], "hostnames": []}
    ok, notes = bridge_rotate.check_candidate(
        "203.0.113.1", "root", Path("k"), exit_ip="198.51.100.9", chain_port=8443, cert=Path("c"),
        privkey=Path("p"), host="lk.example.ru", runner=_runner(), fetch=lambda ip: (dirty, ""))
    assert not ok and len(notes) == 1 and notes[0].startswith("FAIL  reputation")


def test_rotate_bridge_picks_the_tool_by_how_the_bridge_is_run(monkeypatch):
    listed = {"out": ""}
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=listed["out"], stderr=""))
    manual = {"bridge_manual": {"ip": "198.51.100.1", "ssh_user": "root", "provider": "VDSina"}}
    assert cli._bridge_kind(manual) == "vdsina"
    assert cli._bridge_kind({"bridge_manual": {"ip": "198.51.100.1", "provider": "other"}}) == "manual"
    assert cli._bridge_kind({}) == "none"
    listed["out"] = "module.bridge[0].yandex_compute_instance.bridge\n"
    assert cli._bridge_kind(manual) == "yandex"
