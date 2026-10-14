"""Run the real Ansible transaction locally; emulate only the systemd/Xray processes.

All config/state paths are temporary. A loopback listener stands in for Xray's port.
No installed unit, remote server, or repository state is modified.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from test_bridge_config import PASSWORD, render_bridge

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "ansible/roles/bridge_xray_relay"

SERVICE = r'''
import json, os, sys
from pathlib import Path
root = Path(__file__).parent
state_path = root / "service.json"
state = json.loads(state_path.read_text())
action = sys.argv[1]
with (root / "commands.jsonl").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
if action in ("is-active", "is-enabled"):
    value = state["active" if action == "is-active" else "enabled"]
    print(value)
    sys.exit(0 if value in ("active", "enabled") else 3)
if action in ("start", "restart"):
    cfg = json.loads((root / "config.json").read_text())
    if os.environ["BRIDGE_TEST_SCENARIO"] == "activation_failure" and cfg["log"]["loglevel"] != "error":
        state["active"] = "failed"
        state_path.write_text(json.dumps(state))
        sys.exit(1)
    state["active"] = "active"
elif action == "stop":
    state["active"] = "inactive"
elif action in ("enable", "disable"):
    state["enabled"] = "enabled" if action == "enable" else "disabled"
else:
    sys.exit(2)
state_path.write_text(json.dumps(state))
'''

VALIDATOR = r'''
import json, os, sys
json.load(open(sys.argv[-1]))
sys.exit(1 if os.environ["BRIDGE_TEST_SCENARIO"] == "validation_failure" else 0)
'''


def executable(path, source):
    path.write_text(f"#!{sys.executable}\n{source}")
    path.chmod(0o755)


@pytest.fixture
def apply_env(tmp_path):
    assert shutil.which("ansible-playbook"), "install orchestrator[dev] for Ansible regression tests"
    role = tmp_path / "roles/bridge_xray_relay"
    shutil.copytree(ROLE, role)
    executable(tmp_path / "systemctl", SERVICE)
    executable(tmp_path / "xray", VALIDATOR)
    service = {"active": "active", "enabled": "enabled"}
    (tmp_path / "service.json").write_text(json.dumps(service))
    old_config = render_bridge("ss")
    old_config["log"]["loglevel"] = "error"
    # Indented on purpose: a rollback must restore the bytes, not an equivalent re-dump.
    (tmp_path / "config.json").write_text(json.dumps(old_config, indent=3))
    (tmp_path / "config.json").chmod(0o640)
    for name in ("bridge_identity.json", "bridge_runtime.json"):
        (tmp_path / name).write_text(json.dumps({"previous": name}, indent=3))
        (tmp_path / name).chmod(0o600)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        values = {
            "pipeline_state_dir": str(tmp_path),
            "bridge_xray_config": str(tmp_path / "config.json"),
            "bridge_xray_binary": str(tmp_path / "xray"),
            "bridge_systemctl": str(tmp_path / "systemctl"),
            "bridge_start_timeout": 1,
            "bridge_reality_port": port,
            "bridge_client_uuid": "e11fd630-493a-4181-b514-8e462ce601e5",
            "bridge_private_key": "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE",
            "bridge_public_key": "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI",
            "bridge_short_id": "aabbccdd",
            "bridge_reality_sni": "example.com", "bridge_reality_dest": "example.com:443",
            "bridge_ss_method": "2022-blake3-aes-128-gcm", "bridge_ss_password": PASSWORD,
            "exit_state": {
                "exit_ip": "192.0.2.1", "chain_port": 8443,
                "chain_uuid": "6aac169a-291c-49d8-85fc-cf3c4e22e7f2",
                "chain_path": "/restored-chain-path",
                "public_key": "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI",
                "short_id": "11223344", "reality_sni": "example.org",
                "ss_method": "2022-blake3-aes-128-gcm", "ss_password": PASSWORD, "ss_port": 8388,
            },
        }
        (tmp_path / "inventory.ini").write_text(
            f"[bridge]\nbridge_node ansible_host=127.0.0.1 ansible_connection=local "
            f"ansible_python_interpreter={sys.executable}\n"
        )
        (tmp_path / "play.yml").write_text(yaml.safe_dump([{
            "hosts": "bridge", "gather_facts": False, "vars": values,
            "tasks": [{"ansible.builtin.import_role": {
                "name": "bridge_xray_relay", "tasks_from": "apply",
            }}],
        }]))
        (tmp_path / "ansible.cfg").write_text(
            "[defaults]\nretry_files_enabled = False\ninterpreter_python = auto_silent\n"
        )
        yield tmp_path, role, port


def run_apply(root, scenario="success"):
    result = subprocess.run(
        ["ansible-playbook", "-i", str(root / "inventory.ini"), str(root / "play.yml")],
        cwd=root, env={**os.environ, "ANSIBLE_CONFIG": str(root / "ansible.cfg"),
                       "ANSIBLE_LOCAL_TEMP": str(root / "ansible-tmp"),
                       "ANSIBLE_NOCOLOR": "1", "BRIDGE_TEST_SCENARIO": scenario},
        capture_output=True, text=True, timeout=90,
    )
    # Both task output and diff must keep client credentials out of logs.
    assert "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE" not in result.stdout + result.stderr
    return result


def snapshot(root):
    return {name: ((root / name).read_bytes(), (root / name).stat().st_mode & 0o777)
            for name in ("config.json", "bridge_identity.json", "bridge_runtime.json")}


def commands(root):
    return [json.loads(line)[0] for line in (root / "commands.jsonl").read_text().splitlines()]


def test_apply_publishes_state_and_repeat_does_not_restart(apply_env):
    root, _, _ = apply_env
    first = run_apply(root)
    assert first.returncode == 0, first.stdout + first.stderr
    assert commands(root).count("restart") == 1
    saved = snapshot(root)
    outbound = next(o for o in json.loads(saved["config.json"][0])["outbounds"] if o["tag"] == "chain-to-exit")
    assert outbound["streamSettings"]["xhttpSettings"]["path"] == "/restored-chain-path"
    assert json.loads(saved["bridge_identity.json"][0])["short_id"] == "aabbccdd"
    assert json.loads(saved["bridge_runtime.json"][0])["bridge_ip"] == "127.0.0.1"
    second = run_apply(root)
    assert second.returncode == 0, second.stdout + second.stderr
    assert snapshot(root) == saved
    assert commands(root).count("restart") == 1


@pytest.mark.parametrize("scenario", ["validation_failure", "activation_failure", "state_failure"])
def test_failure_restores_config_service_and_controller_state(apply_env, scenario):
    root, role, _ = apply_env
    saved = snapshot(root)
    if scenario == "state_failure":
        # Let runtime publish succeed, then fail the second state template.
        (role / "templates/bridge_identity.json.j2").write_text("{{ deliberately_missing_variable }}")
    result = run_apply(root, scenario)
    assert result.returncode != 0
    assert "Bridge apply failed" in result.stdout, result.stdout + result.stderr
    assert snapshot(root) == saved
    assert json.loads((root / "service.json").read_text()) == {
        "active": "active", "enabled": "enabled",
    }
    assert commands(root).count("restart") == (0 if scenario == "validation_failure" else 2)


def test_fresh_failed_apply_removes_new_config_and_state(apply_env):
    root, role, _ = apply_env
    for name in ("config.json", "bridge_identity.json", "bridge_runtime.json"):
        (root / name).unlink()
    old_service = {"active": "inactive", "enabled": "disabled"}
    (root / "service.json").write_text(json.dumps(old_service))
    (role / "templates/bridge_identity.json.j2").write_text("{{ deliberately_missing_variable }}")
    result = run_apply(root, "state_failure")
    assert result.returncode != 0
    assert "Bridge apply failed" in result.stdout, result.stdout + result.stderr
    assert not any((root / name).exists() for name in (
        "config.json", "bridge_identity.json", "bridge_runtime.json",
    ))
    assert json.loads((root / "service.json").read_text()) == old_service
    assert commands(root).count("stop") == 1
