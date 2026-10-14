"""Run the real exit_firewall role and WORLD helper locally against emulated ufw/systemd/ssh.

The emulation keeps "rule files" and a separate "loaded" ruleset under a temporary root, so
snapshot, restore and the lockout guard are checked against what would actually be in effect.
No real firewall, unit or remote host is touched.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from test_bridge_apply import executable

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "ansible/roles/exit_firewall"
CONTROLLER, BRIDGE = "203.0.113.7", "198.51.100.2"

FAKE = r'''
import json, os, sys
from pathlib import Path
base = Path(__file__).resolve().parent.parent
root = base / "root"
tool, args = Path(sys.argv[0]).name, sys.argv[1:]
scenario = os.environ.get("EXIT_FW_TEST_SCENARIO", "")
with (base / "commands.jsonl").open("a") as log:
    log.write(json.dumps([tool, *args]) + "\n")
rules, conf, defaults = (root / "etc/ufw/user.rules", root / "etc/ufw/ufw.conf",
                         root / "etc/default/ufw")
kernel_path, guard = base / "kernel.json", base / "guard.json"
kernel = json.loads(kernel_path.read_text())

def save():
    kernel_path.write_text(json.dumps(kernel))

def setting(key, value=None):
    lines = defaults.read_text().splitlines()
    if value is None:
        return next((l.split("=", 1)[1].strip('"') for l in lines if l.startswith(key + "=")), "")
    defaults.write_text("\n".join(
        f'{key}="{value}"' if l.startswith(key + "=") else l for l in lines) + "\n")

if tool == "ssh":
    sys.exit(255 if scenario in ("ssh_failure", "lockout") else 0)
if tool == "systemd-run":
    guard.write_text(json.dumps({"armed": True, "argv": [a for a in args if not a.startswith("--on-")
                                                          and not a.startswith("--unit")
                                                          and not a.startswith("--description")]}))
    sys.exit(0)
if tool == "systemctl":
    if args[0] == "stop" and guard.exists():
        guard.write_text(json.dumps({**json.loads(guard.read_text()), "armed": False}))
    sys.exit(0)
if tool in ("iptables", "ip6tables"):
    drop = kernel["loaded"] and kernel["policy"] == "DROP" and (tool == "iptables" or kernel["ipv6"])
    print("-P INPUT DROP" if drop else "-P INPUT ACCEPT")
    sys.exit(0)
assert tool == "ufw", tool
if args[:1] == ["status"]:
    print("Status: active" if kernel["loaded"] else "Status: inactive")
    if kernel["loaded"]:
        if args[1:] == ["verbose"]:
            print(f"Default: {'deny' if kernel['policy'] == 'DROP' else 'allow'} (incoming), allow (outgoing)")
        for number, rule in enumerate(kernel["rules"], 1):
            print(f"[{number}] {rule}")
elif args == ["--force", "reset"]:
    for name in ("user.rules", "user6.rules"):
        (root / "etc/ufw" / name).write_text("# rules\n")
    conf.write_text("ENABLED=no\n")
    kernel.update(loaded=False, rules=[])
elif args[:1] == ["default"]:
    setting("DEFAULT_INPUT_POLICY" if args[2] == "incoming" else "DEFAULT_OUTPUT_POLICY",
            "DROP" if args[1] == "deny" else "ACCEPT")
elif args[:1] == ["allow"]:
    if scenario == "rule_failure" and "bridge chain" in args:
        print("ERROR: simulated rule failure", file=sys.stderr)
        sys.exit(1)
    for name in ("user.rules", "user6.rules"):
        with (root / "etc/ufw" / name).open("a") as stream:
            stream.write(" ".join(args) + "\n")
elif args == ["--force", "enable"]:
    conf.write_text("ENABLED=yes\n")
    kernel.update(loaded=True, rules=rules.read_text().splitlines()[1:],
                  policy=setting("DEFAULT_INPUT_POLICY"),
                  ipv6=setting("IPV6") == "yes" and scenario != "ipv6_unmanaged")
elif args == ["disable"]:
    if scenario == "lockout":
        print("ERROR: simulated disable failure", file=sys.stderr)
        sys.exit(1)
    conf.write_text("ENABLED=no\n")
    kernel.update(loaded=False, rules=[])
else:
    sys.exit(2)
save()
'''


@pytest.fixture
def fw_env(tmp_path):
    assert shutil.which("ansible-playbook"), "install orchestrator[dev] for Ansible regression tests"
    shutil.copytree(ROLE, tmp_path / "roles/exit_firewall")
    (tmp_path / "bin").mkdir()
    for tool in ("ufw", "iptables", "ip6tables", "systemctl", "systemd-run", "ssh"):
        executable(tmp_path / "bin" / tool, FAKE)
    root = tmp_path / "root"
    (root / "etc/ufw").mkdir(parents=True)
    (root / "etc/default").mkdir()
    (root / "proc/net").mkdir(parents=True)
    (root / "proc/net/if_inet6").write_text("00000000000000000000000000000001 01 80 10 80 lo\n")
    for name in ("user.rules", "user6.rules", "before.rules", "before6.rules",
                 "after.rules", "after6.rules"):
        (root / "etc/ufw" / name).write_text("# rules\n")
    (root / "etc/ufw/ufw.conf").write_text("ENABLED=no\n")
    (root / "etc/default/ufw").write_text(
        'IPV6=no\nDEFAULT_INPUT_POLICY="ACCEPT"\nDEFAULT_OUTPUT_POLICY="ACCEPT"\n')
    (root / "etc/default/ufw").chmod(0o644)
    (tmp_path / "kernel.json").write_text(json.dumps(
        {"loaded": False, "rules": [], "policy": "ACCEPT", "ipv6": False}))
    values = {
        "ansible_ssh_private_key_file": str(tmp_path / "unused-ssh-key"),
        "exit_fw_root": str(root), "exit_fw_state_dir": str(tmp_path / "fw-state"),
        "exit_fw_remote_helper": str(tmp_path / "libexec/firewall_remote.py"),
        "exit_fw_python": sys.executable, "exit_fw_install_package": False,
        "exit_fw_fresh_ssh_delay": 0, "exit_fw_safety_seconds": 77,
        "exit_fw_bridge_ip": BRIDGE, "exit_fw_direct_policy": "public",
    }
    (tmp_path / "inventory.ini").write_text(
        "[exit]\nexit_node ansible_host=127.0.0.1 ansible_connection=local ansible_user=tester "
        f"ansible_python_interpreter={sys.executable}\n"
    )
    (tmp_path / "play.yml").write_text(yaml.safe_dump([{
        "hosts": "exit", "gather_facts": False, "vars": values,
        "roles": [{"role": "exit_firewall"}],
    }]))
    (tmp_path / "ansible.cfg").write_text(
        "[defaults]\nretry_files_enabled = False\ninterpreter_python = auto_silent\n"
    )
    return tmp_path


def environment(base, scenario=""):
    return {**os.environ, "PATH": str(base / "bin") + os.pathsep + os.environ["PATH"],
            "ANSIBLE_CONFIG": str(base / "ansible.cfg"),
            "ANSIBLE_LOCAL_TEMP": str(base / "ansible-tmp"), "ANSIBLE_NOCOLOR": "1",
            "SSH_CONNECTION": f"{CONTROLLER} 50000 192.0.2.10 22",
            "EXIT_FW_TEST_SCENARIO": scenario}


def run_role(base, scenario="", **overrides):
    (base / "overrides.json").write_text(json.dumps(overrides))
    return subprocess.run(
        ["ansible-playbook", "-i", str(base / "inventory.ini"), "-e", f"@{base / 'overrides.json'}",
         str(base / "play.yml")],
        cwd=base, env=environment(base, scenario), capture_output=True, text=True, timeout=120,
    )


def effective(base):
    """Rule files plus the loaded ruleset: what a restore must bring back exactly."""
    files = {p.name: p.read_text() for p in sorted((base / "root/etc/ufw").iterdir())}
    files["default"] = (base / "root/etc/default/ufw").read_text()
    return files, json.loads((base / "kernel.json").read_text())


def loaded(base):
    return json.loads((base / "kernel.json").read_text())


def commands(base, tool):
    log = base / "commands.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [json.loads(line)[1:] for line in lines if json.loads(line)[0] == tool]


def snapshots(base):
    directory = base / "fw-state/snapshots"
    return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []


def armed(base):
    return json.loads((base / "guard.json").read_text())["armed"]


def test_apply_separates_chain_from_direct_and_repeat_is_a_noop(fw_env):
    first = run_role(fw_env)
    assert first.returncode == 0, first.stdout + first.stderr
    kernel = loaded(fw_env)
    assert kernel["loaded"] and kernel["policy"] == "DROP" and kernel["ipv6"]
    assert "allow 443/tcp comment direct public" in kernel["rules"]
    assert "allow 8388/udp comment direct public" in kernel["rules"]
    assert f"allow from {BRIDGE} to any port 8443 proto tcp comment bridge chain" in kernel["rules"]
    assert f"allow from {CONTROLLER} to any port 22 proto tcp comment controller ssh" in kernel["rules"]
    assert not any(rule.startswith("allow 8443") or "2053" in rule for rule in kernel["rules"])
    assert not armed(fw_env)
    fresh = commands(fw_env, "ssh")[-1]
    assert "tester@127.0.0.1" in fresh and fresh[fresh.index("-p") + 1] == "22"
    assert "ControlPath=none" in fresh
    assert len(snapshots(fw_env)) == 1  # the firewall that preceded the first apply

    saved = effective(fw_env)
    second = run_role(fw_env)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "changed=0" in second.stdout
    assert effective(fw_env) == saved
    assert len(snapshots(fw_env)) == 1


def test_rollback_restores_an_earlier_firewall_and_is_repeatable(fw_env):
    assert run_role(fw_env).returncode == 0
    public = effective(fw_env)
    changed = run_role(fw_env, exit_fw_direct_policy="closed")
    assert changed.returncode == 0, changed.stdout + changed.stderr
    assert "allow 443/tcp comment direct public" not in loaded(fw_env)["rules"]
    first, latest = snapshots(fw_env)

    for _ in range(2):
        result = run_role(fw_env, exit_fw_action="rollback", exit_fw_direct_policy="")
        assert result.returncode == 0, result.stdout + result.stderr
        assert effective(fw_env) == public
        assert snapshots(fw_env) == [first, latest]
        assert not armed(fw_env)

    # The oldest snapshot is the host before this role ever ran: ufw inactive.
    result = run_role(fw_env, exit_fw_action="rollback", exit_fw_snapshot=first)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not loaded(fw_env)["loaded"]
    assert "IPV6=no" in (fw_env / "root/etc/default/ufw").read_text()

    missing = run_role(fw_env, exit_fw_action="rollback", exit_fw_snapshot="20000101T000000Z")
    assert missing.returncode != 0
    assert "snapshot not found" in missing.stdout
    assert not loaded(fw_env)["loaded"] and not armed(fw_env)
    assert snapshots(fw_env) == [first, latest]


@pytest.mark.parametrize("scenario", ["rule_failure", "ssh_failure", "ipv6_unmanaged"])
def test_failed_change_restores_the_previous_firewall(fw_env, scenario):
    assert run_role(fw_env).returncode == 0
    saved, history = effective(fw_env), snapshots(fw_env)
    result = run_role(fw_env, scenario, exit_fw_direct_policy="closed")
    assert result.returncode != 0
    assert "the previous rules were restored" in result.stdout, result.stdout + result.stderr
    if scenario == "ipv6_unmanaged":
        saved[1]["ipv6"] = False  # the emulated fault also affects the restoring enable
    assert effective(fw_env) == saved
    assert snapshots(fw_env) == history
    assert not armed(fw_env)


def test_unrestorable_lockout_leaves_the_guard_armed_and_the_guard_restores(fw_env):
    assert run_role(fw_env).returncode == 0
    saved = effective(fw_env)
    result = run_role(fw_env, "lockout", exit_fw_direct_policy="closed")
    assert result.returncode != 0
    guard = json.loads((fw_env / "guard.json").read_text())
    assert guard["armed"] and guard["argv"][-3] == "restore" and guard["argv"][-1] == "--failsafe"
    assert effective(fw_env) != saved
    # What systemd would run once the timer fires on WORLD.
    fired = subprocess.run(guard["argv"], env=environment(fw_env), capture_output=True, text=True)
    assert fired.returncode == 0, fired.stderr
    assert effective(fw_env) == saved


def test_guard_falls_back_to_disable_when_the_snapshot_is_unusable(fw_env):
    assert run_role(fw_env).returncode == 0
    helper = [sys.executable, str(ROLE / "files/firewall_remote.py"),
              "--state", str(fw_env / "fw-state"), "--root", str(fw_env / "root")]
    fired = subprocess.run(helper + ["restore", "20000101T000000Z", "--failsafe"],
                           env=environment(fw_env), capture_output=True, text=True)
    assert fired.returncode != 0 and "snapshot not found" in fired.stderr
    assert not loaded(fw_env)["loaded"]
    plain = subprocess.run(helper + ["apply"], input='{"rules": [["allow", "--force"]]}',
                           env=environment(fw_env), capture_output=True, text=True)
    assert plain.returncode != 0 and "malformed" in plain.stderr


@pytest.mark.parametrize("overrides, message", [
    ({"exit_fw_direct_policy": ""}, "no default"),
    ({"exit_fw_bridge_ip": ""}, "direct-only"),
    ({"exit_fw_direct_only": True}, "conflicts"),
    ({"exit_fw_direct_policy": "allowlist"}, "at least one"),
    ({"exit_fw_extra_admin_cidrs": ["0.0.0.0/0"]}, "whole internet"),
    ({"chain_port": 443}, "distinct"),
])
def test_refused_plan_touches_nothing(fw_env, overrides, message):
    result = run_role(fw_env, **overrides)
    assert result.returncode != 0
    assert message in result.stdout + result.stderr
    assert commands(fw_env, "ufw") == [] and commands(fw_env, "systemd-run") == []
    assert snapshots(fw_env) == []
    assert not (fw_env / "libexec").exists()


def test_explicit_direct_only_with_allowlist_and_admin_sources(fw_env):
    result = run_role(
        fw_env, exit_fw_bridge_ip="", exit_fw_direct_only=True, exit_fw_direct_policy="allowlist",
        exit_fw_direct_cidrs=["192.0.2.0/24", "2001:db8::/48"],
        exit_fw_extra_admin_cidrs=["192.0.2.50/32"], chain_port=9443,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rules = loaded(fw_env)["rules"]
    assert not any(BRIDGE in rule for rule in rules)
    assert "allow from 2001:db8::/48 to any port 443 proto tcp comment direct allowlist" in rules
    assert "allow from 192.0.2.50 to any port 22 proto tcp comment admin ssh" in rules
    assert [rule for rule in rules if " 9443 " in rule] == [
        f"allow from {CONTROLLER} to any port 9443 proto tcp comment controller validate"]
    assert not any(rule.startswith("allow 443") or rule.startswith("allow 22") for rule in rules)
