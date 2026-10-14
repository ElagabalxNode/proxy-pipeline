"""Run offline artifact delivery and install tasks without touching the real system."""

import json
import os
import pwd
import shutil
import subprocess
import sys

import pytest
import yaml

from test_bridge_apply import ROLE, executable
from test_bridge_support import archive_at

SYSTEMCTL = r'''
import json, sys
from pathlib import Path
root = Path(__file__).parent
args = sys.argv[1:]
with (root / "commands.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\n")
if args == ["daemon-reload"]:
    sys.exit(0)
unit = root / "xray.service"
if args == ["show", "xray.service", "--property=LoadState", "--value"]:
    print("loaded" if unit.exists() else "not-found")
    sys.exit(0 if unit.exists() else 4)
elif args == ["show", "xray.service", "--property=ExecStart", "--value"]:
    command = next(line.removeprefix("ExecStart=") for line in unit.read_text().splitlines()
                   if line.startswith("ExecStart="))
    print("{ path=" + command.split()[0] + " ; argv[]=" + command + " ; ignore_errors=no ; }")
else:
    sys.exit(1)
'''


def run_install(root, values):
    (root / "play.yml").write_text(yaml.safe_dump([{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {**values, "ansible_python_interpreter": sys.executable},
        "tasks": [{"ansible.builtin.import_role": {
            "name": "bridge_xray_relay", "tasks_from": "install",
        }}],
    }]))
    return subprocess.run(
        ["ansible-playbook", "-i", "localhost,", str(root / "play.yml")],
        cwd=root, env={**os.environ, "ANSIBLE_CONFIG": str(root / "ansible.cfg"),
                       "ANSIBLE_LOCAL_TEMP": str(root / "ansible-tmp"),
                       "ANSIBLE_NOCOLOR": "1"},
        capture_output=True, text=True, timeout=90,
    )


@pytest.fixture
def install_env(tmp_path):
    shutil.copytree(ROLE, tmp_path / "roles/bridge_xray_relay")
    (tmp_path / "ansible.cfg").write_text("[defaults]\nretry_files_enabled = False\n")
    executable(tmp_path / "systemctl", SYSTEMCTL)
    archive = tmp_path / "offline.zip"
    checksum = archive_at(archive)
    values = {
        "bridge_xray_version": "25.10.15", "bridge_xray_archive_sha256": checksum,
        "bridge_xray_archive_path": str(archive), "pipeline_state_dir": str(tmp_path / "state"),
        "bridge_xray_cache_dir": str(tmp_path / "cache"),
        "bridge_xray_binary": str(tmp_path / "bin/xray"),
        "bridge_xray_config": str(tmp_path / "etc/config.json"),
        "bridge_support_path": str(tmp_path / "lib/bridge_support.py"),
        "bridge_xray_asset_dir": str(tmp_path / "assets"),
        "bridge_xray_log_dir": str(tmp_path / "logs"),
        "bridge_xray_unit_path": str(tmp_path / "xray.service"),
        "bridge_xray_service_user": pwd.getpwuid(os.getuid()).pw_name,
        "bridge_systemctl": str(tmp_path / "systemctl"),
    }
    return tmp_path, values


def test_offline_install_and_repeat_preserve_binary_unit_and_geodata(install_env):
    root, values = install_env
    first = run_install(root, values)
    assert first.returncode == 0, first.stdout + first.stderr
    binary = (root / "bin/xray").read_bytes()
    assert (root / "logs/access.log").stat().st_mode & 0o777 == 0o600
    unit = (root / "xray.service").read_text() + "\n# operator customization\n"
    (root / "xray.service").write_text(unit)
    (root / "assets/geoip.dat").write_bytes(b"operator data")
    second = run_install(root, values)
    assert second.returncode == 0, second.stdout + second.stderr
    assert (root / "bin/xray").read_bytes() == binary
    assert (root / "xray.service").read_text() == unit
    assert (root / "assets/geoip.dat").read_bytes() == b"operator data"
    calls = [json.loads(line)[0] for line in (root / "commands.jsonl").read_text().splitlines()]
    assert calls.count("daemon-reload") == 1
    assert set(calls) == {"show", "daemon-reload"}  # No activation during preparation.
    assert not (root / "etc/config.json").exists()


@pytest.mark.parametrize("failure", ["checksum", "version", "existing_binary", "unit"])
def test_install_refuses_invalid_artifact_and_implicit_upgrade(install_env, failure):
    root, values = install_env
    if failure == "checksum":
        values["bridge_xray_archive_sha256"] = "0" * 64
    elif failure == "version":
        values["bridge_xray_version"] = "1.2.3"
    elif failure == "existing_binary":
        (root / "bin").mkdir()
        (root / "bin/xray").write_bytes(b"existing binary must survive")
    else:
        (root / "xray.service").write_text("ExecStart=/custom/xray run -config /custom/config.json\n")
    result = run_install(root, values)
    assert result.returncode != 0
    if failure == "existing_binary":
        assert (root / "bin/xray").read_bytes() == b"existing binary must survive"
    elif failure != "unit":
        assert not (root / "bin/xray").exists()
    if failure == "unit":
        assert "unsupported binary/config/command" in result.stdout
        assert (root / "xray.service").read_text().startswith("ExecStart=/custom/xray")
    assert not (root / "etc/config.json").exists()
