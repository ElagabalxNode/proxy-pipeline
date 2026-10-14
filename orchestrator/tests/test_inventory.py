"""Read-only node inventory: the collector against emulated tools, pins and rendering."""

import hashlib
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from pipelinectl import cli, inventory
from test_bridge_apply import executable

TOOLS = r'''
import sys
from pathlib import Path
tool, args = Path(sys.argv[0]).name, sys.argv[1:]
with (Path(__file__).parent / "calls.log").open("a") as log:
    log.write(" ".join([tool, *args]) + "\n")
if tool == "ss":
    print('tcp LISTEN 0 4096 0.0.0.0:443 0.0.0.0:* users:(("xray-linux-amd6",pid=7,fd=3))')
    print('tcp LISTEN 0 128 [::]:22 [::]:* users:(("sshd",pid=1,fd=4))')
    print('udp UNCONN 0 0 *:8388 *:* users:(("xray-linux-amd6",pid=7,fd=9))')
elif tool == "sshd":
    print("port 22\npasswordauthentication yes\npermitrootlogin yes\nmaxstartups 10:30:100")
    print("hostkey /etc/ssh/ssh_host_ed25519_key\npubkeyauthentication yes")
elif tool == "systemctl":
    print("active" if args[1] in ("x-ui", "fail2ban") else "inactive")
elif tool == "fail2ban-client":
    print("Status for the jail: sshd\n|- Currently failed: 3\n`- Currently banned:\t12")
elif tool == "ufw":
    print("Status: inactive")
elif tool == "ip":
    print("2: eth0    inet6 2001:db8::7/64 scope global \\       valid_lft forever")
elif tool == "x-ui":
    print("2.6.0")
elif tool.startswith("xray"):
    print("Xray 25.10.15 (Xray, Penetrates Everything.) abc (go1.25 linux/amd64)\nA unified platform")
'''


@pytest.fixture
def node_tools(tmp_path, monkeypatch):
    for tool in ("ss", "sshd", "systemctl", "fail2ban-client", "ufw", "ip", "x-ui",
                 "xray-linux-amd64"):
        executable(tmp_path / tool, TOOLS)
    monkeypatch.setenv("PATH", str(tmp_path), prepend=":")
    options = {"xray": [str(tmp_path / "xray-linux-*")], "panel": str(tmp_path / "x-ui"),
               "release_marker": str(tmp_path / "missing-marker"),
               "services": ["x-ui", "ufw", "fail2ban"]}
    monkeypatch.setitem(inventory.ROLES, "exit", options)
    return tmp_path


def local_runner(cmd, **kwargs):
    """Stand in for ssh: run the remote command line locally, with the same stdin."""
    assert cmd[0] == "ssh" and "BatchMode=yes" in cmd and cmd[-2] == "root@192.0.2.10"
    remote = cmd[-1].replace("python3", sys.executable, 1)
    return subprocess.run(["bash", "-c", remote], **kwargs)


def test_collector_reads_versions_listeners_and_ssh_posture(node_tools):
    node = inventory.collect("192.0.2.10", "root", node_tools / "key", "exit", runner=local_runner)
    assert "error" not in node, node
    binary = node_tools / "xray-linux-amd64"
    assert node["xray"] == [{
        "path": str(binary), "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "version": "Xray 25.10.15 (Xray, Penetrates Everything.) abc (go1.25 linux/amd64)"}]
    assert node["xui"] == {"version": "2.6.0", "installed_pin": None}
    assert {"proto": "tcp", "address": "0.0.0.0", "port": "443", "process": "xray-linux-amd6"} in node["listeners"]
    assert {"proto": "tcp", "address": "[::]", "port": "22", "process": "sshd"} in node["listeners"]
    assert node["sshd"] == {"port": "22", "passwordauthentication": "yes", "permitrootlogin": "yes",
                            "maxstartups": "10:30:100", "pubkeyauthentication": "yes"}
    assert node["services"] == {"x-ui": "active", "ufw": "inactive", "fail2ban": "active"}
    assert node["fail2ban_sshd"] == {"jail": True, "currently_banned": 12}
    assert node["ufw"]["status"] == ["Status: inactive"]
    assert node["ipv6_global"] == ["2001:db8::7/64"]
    assert abs(node["clock_skew_seconds"]) < 5
    # Read-only: only status/version queries reached the tools.
    calls = (node_tools / "calls.log").read_text().splitlines()
    assert sorted(set(calls)) == sorted({
        "ss -H -tulnp", "sshd -T", "systemctl is-active x-ui", "systemctl is-active ufw",
        "systemctl is-active fail2ban", "fail2ban-client status sshd", "ufw status verbose",
        "ip -6 -o addr show scope global", "x-ui -v", "xray-linux-amd64 version"})


def test_unreachable_node_is_reported_not_raised(tmp_path):
    def refused(cmd, **kwargs):
        return SimpleNamespace(returncode=255, stdout="", stderr="ssh: connect: Connection refused\n")
    node = inventory.collect("192.0.2.10", "root", tmp_path / "key", "exit", runner=refused)
    assert node == {"role": "exit", "address": "192.0.2.10",
                    "error": "ssh: connect: Connection refused"}

    def sudo(cmd, **kwargs):
        assert cmd[-1].startswith("sudo -n python3 - ")
        raise subprocess.TimeoutExpired(cmd, 120)
    assert "error" in inventory.collect("192.0.2.11", "ubuntu", tmp_path / "key", "bridge", runner=sudo)


def test_pin_status_and_render(node_tools):
    node = inventory.collect("192.0.2.10", "root", node_tools / "key", "exit", runner=local_runner)
    sha = node["xray"][0]["sha256"]
    assert [row[3] for row in inventory.pin_status(node, {})] == ["unpinned"] * 3
    cfg = {"xray": {"world_release": {"xui_version": "2.6.0", "xray_version": "25.10.14",
                                      "xray_sha256": sha}}}
    assert [row[3] for row in inventory.pin_status(node, cfg)] == ["match", "MISMATCH", "match"]
    bridge = {"role": "bridge", "address": "198.51.100.2", "xray": [
        {"path": "/usr/local/bin/xray", "version": "Xray 25.10.15 (x)", "sha256": "f" * 64}]}
    cfg["xray"]["bridge_release"] = {"version": "25.10.15"}
    assert inventory.pin_status(bridge, cfg) == [("Xray version", "25.10.15", "25.10.15", "match")]
    text = inventory.render([node, {"role": "bridge", "address": "198.51.100.2", "error": "timeout"}], cfg)
    assert "pin    Xray version: installed 25.10.15  pinned 25.10.14  -> MISMATCH" in text
    assert "passwordauthentication=yes" in text and "tcp 0.0.0.0:443  xray-linux-amd6" in text
    assert "UNREACHABLE: timeout" in text


def test_inventory_command_uses_state_and_flags_unreachable_nodes(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_cfg", lambda: {})
    monkeypatch.setattr(cli.paths, "state_dir", lambda cfg: tmp_path)
    (tmp_path / "runtime.json").write_text('{"exit_ip": "192.0.2.10"}')
    (tmp_path / "bridge_runtime.json").write_text(
        '{"bridge_ip": "198.51.100.2", "bridge_ssh_user": "root"}')
    seen = []

    def collect(host, user, key, role):
        seen.append((host, user, role))
        return {"role": role, "address": host, "error": "timeout"}
    monkeypatch.setattr(cli.inventory, "collect", collect)
    result = CliRunner().invoke(cli.app, ["inventory", "--format", "json"])
    assert result.exit_code == 2
    assert seen == [("192.0.2.10", "root", "exit"), ("198.51.100.2", "root", "bridge")]
    assert [n["role"] for n in json.loads(result.output)] == ["exit", "bridge"]
    seen.clear()
    CliRunner().invoke(cli.app, ["inventory", "--node", "bridge"])
    assert seen == [("198.51.100.2", "root", "bridge")]
