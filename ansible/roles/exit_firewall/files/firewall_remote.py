"""WORLD-side ufw snapshot/apply/verify/restore; also what the lockout timer runs.

Snapshots are complete copies of the ufw rule files and defaults, so a failed change, a lost
controller session or an operator rollback all return to a known earlier firewall.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RULE_FILES = ("user.rules", "user6.rules", "before.rules", "before6.rules",
              "after.rules", "after6.rules", "ufw.conf")
UNIT = "exit-fw-safety"
KEEP = 10


class FirewallError(Exception):
    pass


def run(*argv, check=True):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FirewallError(f"{argv[0]} could not run: {exc}") from exc
    if check and proc.returncode:
        detail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["no output"]
        raise FirewallError(f"{' '.join(argv)} failed: {detail[0]}")
    return proc.stdout


def tracked(root):
    files = {name: root / "etc/ufw" / name for name in RULE_FILES}
    files["default"] = root / "etc/default/ufw"
    return files


def active():
    return "Status: active" in run("ufw", "status")


def replace(path, content, like):
    fd, temp = tempfile.mkstemp(prefix=".pipeline-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), like.stat().st_mode & 0o777)
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def names(state):
    directory = state / "snapshots"
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if p.is_dir() and not p.name.startswith("."))


def locate(state, name):
    available = names(state)
    if name == "latest" and available:
        name = available[-1]
    if name not in available:
        raise FirewallError(f"firewall snapshot not found: {name}")
    return state / "snapshots" / name


def snapshot(state, root):
    directory = state / "snapshots"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for stale in directory.glob(".*.tmp"):
        shutil.rmtree(stale)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    name, serial = stamp, 0
    while (directory / name).exists():
        serial += 1
        name = f"{stamp}-{serial}"
    scratch = directory / f".{name}.tmp"
    scratch.mkdir(mode=0o700)
    saved = []
    for key, path in tracked(root).items():
        if path.is_file():
            shutil.copy2(path, scratch / key)
            saved.append(key)
    (scratch / "meta.json").write_text(json.dumps({"active": active(), "files": saved}))
    scratch.rename(directory / name)
    return {"snapshot": name}


def restore(state, root, name):
    source = locate(state, name)
    meta = json.loads((source / "meta.json").read_text())
    for key, path in tracked(root).items():
        if key in meta["files"]:
            replace(path, (source / key).read_bytes(), source / key)
    # Unload whatever is running, then load exactly the restored files.
    run("ufw", "disable")
    if meta["active"]:
        run("ufw", "--force", "enable")
    return {"restored": source.name, "active": meta["active"]}


def manage_ipv6(root):
    """Without IPV6=yes ufw leaves ip6tables untouched: every port stays open over IPv6."""
    path = root / "etc/default/ufw"
    text = path.read_text()
    if re.search(r"^IPV6=yes\s*$", text, re.M):
        return
    updated, count = re.subn(r"^IPV6=.*$", "IPV6=yes", text, flags=re.M)
    if not count:
        updated = text.rstrip("\n") + "\nIPV6=yes\n"
    replace(path, updated.encode(), path)


def apply(root, rules):
    if not isinstance(rules, list) or not rules or any(
        not isinstance(rule, list) or rule[:1] != ["allow"]
        or any(not isinstance(arg, str) or arg.startswith("-") for arg in rule) for rule in rules
    ):
        raise FirewallError("refusing an empty or malformed rule plan")
    manage_ipv6(root)
    run("ufw", "--force", "reset")
    run("ufw", "default", "deny", "incoming")
    run("ufw", "default", "allow", "outgoing")
    for rule in rules:
        run("ufw", *rule)
    run("ufw", "--force", "enable")
    return {"rules": len(rules)}


def verify(root):
    status = run("ufw", "status", "verbose")
    if "Status: active" not in status or "deny (incoming)" not in status:
        raise FirewallError("ufw is not active with default deny for incoming traffic")
    if not re.search(r"^IPV6=yes\s*$", (root / "etc/default/ufw").read_text(), re.M):
        raise FirewallError("ufw does not manage IPv6")
    inet6 = root / "proc/net/if_inet6"
    families = ["iptables"] + (["ip6tables"] if inet6.is_file() and inet6.read_text().strip() else [])
    for tool in families:
        if "-P INPUT DROP" not in run(tool, "-S", "INPUT").splitlines():
            raise FirewallError(f"{tool} INPUT policy is not DROP after enabling ufw")
    return {"families": families, "status": run("ufw", "status", "numbered").splitlines()}


def commit(state, root, name):
    """Keep the pre-change snapshot only when the change really altered the firewall."""
    source = locate(state, name)
    meta = json.loads((source / "meta.json").read_text())
    files = tracked(root)
    same = meta["active"] == active() and all(
        files[key].is_file() and files[key].read_bytes() == (source / key).read_bytes()
        for key in meta["files"]
    )
    if same:
        shutil.rmtree(source)
    for old in names(state)[:-KEEP]:
        shutil.rmtree(state / "snapshots" / old)
    return {"changed": not same, "snapshots": names(state)}


def discard(state, name):
    shutil.rmtree(locate(state, name))
    return {"snapshots": names(state)}


def disarm():
    run("systemctl", "stop", f"{UNIT}.timer", check=False)
    run("systemctl", "reset-failed", f"{UNIT}.service", check=False)
    return {"armed": False}


def arm(state, root, name, seconds):
    locate(state, name)
    disarm()
    run("systemd-run", f"--on-active={int(seconds)}", f"--unit={UNIT}",
        "--description=proxy-pipeline: restore the firewall if the controller got locked out",
        sys.executable, os.path.abspath(__file__), "--state", str(state), "--root", str(root),
        "restore", name, "--failsafe")
    return {"armed": True, "seconds": int(seconds)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--root", default="/", type=Path)
    parser.add_argument("action")
    parser.add_argument("name", nargs="?", default="latest")
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--failsafe", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "snapshot":
            result = snapshot(args.state, args.root)
        elif args.action == "arm":
            result = arm(args.state, args.root, args.name, args.seconds)
        elif args.action == "disarm":
            result = disarm()
        elif args.action == "apply":
            result = apply(args.root, json.load(sys.stdin)["rules"])
        elif args.action == "verify":
            result = verify(args.root)
        elif args.action == "restore":
            result = restore(args.state, args.root, args.name)
        elif args.action == "commit":
            result = commit(args.state, args.root, args.name)
        elif args.action == "discard":
            result = discard(args.state, args.name)
        elif args.action == "resolve":
            result = {"snapshot": locate(args.state, args.name).name}
        else:
            raise FirewallError(f"unknown firewall action: {args.action}")
    except (FirewallError, OSError, ValueError, KeyError, TypeError) as exc:
        if args.failsafe:
            # The lockout timer could not restore the snapshot: an open host beats a lost one.
            subprocess.run(["ufw", "disable"], check=False)
        print(f"WORLD firewall {args.action} failed: {exc}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
