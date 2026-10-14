"""Read-only inventory of WORLD and RU: installed versions for the pins, listeners, SSH posture.

The collector runs on the node over SSH and only reads: versions, checksums, listening sockets,
effective sshd settings, firewall and fail2ban status. It prints no keys, passwords or configs.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from pipelinectl import world_release

COLLECTOR = r'''
import glob, hashlib, json, os, platform, re, subprocess, sys, time

options = json.loads(sys.argv[1])


def run(*argv):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        return proc.returncode, proc.stdout
    except Exception:
        return None, ""


def lines(*argv):
    return run(*argv)[1].splitlines()


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def key_count(home):
    try:
        with open(os.path.join(home, ".ssh/authorized_keys")) as stream:
            return sum(1 for line in stream if line.strip() and not line.lstrip().startswith("#"))
    except OSError:
        return None


result = {"time": time.time(), "host": platform.node(), "arch": platform.machine(),
          "kernel": platform.release(), "os": "", "xray": [], "listeners": [], "sshd": {},
          "services": {}, "ipv6_global": []}
try:
    with open("/etc/os-release") as stream:
        for line in stream:
            if line.startswith("PRETTY_NAME="):
                result["os"] = line.split("=", 1)[1].strip().strip('"')
except OSError:
    pass

for pattern in options["xray"]:
    for path in sorted(glob.glob(pattern)):
        entry = {"path": path, "version": (lines(path, "version") or [""])[0]}
        try:
            entry["sha256"] = sha256(path)
        except OSError:
            entry["sha256"] = None
        result["xray"].append(entry)

if options.get("panel"):
    marker = None
    try:
        with open(options["release_marker"]) as stream:
            marker = stream.read().strip()
    except OSError:
        pass
    result["xui"] = {"version": (lines(options["panel"], "-v") or [""])[-1].strip(),
                     "installed_pin": marker}

for line in lines("ss", "-H", "-tulnp"):
    columns = line.split()
    if len(columns) < 5 or ":" not in columns[4]:
        continue
    address, port = columns[4].rsplit(":", 1)
    process = re.search(r'\(\("([^"]+)"', line)
    result["listeners"].append({"proto": columns[0], "address": address, "port": port,
                                "process": process.group(1) if process else ""})

wanted = ("port", "passwordauthentication", "kbdinteractiveauthentication",
          "pubkeyauthentication", "permitrootlogin", "maxstartups", "maxauthtries",
          "authenticationmethods")
for line in lines("sshd", "-T"):
    key, _, value = line.partition(" ")
    if key in wanted:
        result["sshd"][key] = value.strip()
result["authorized_keys"] = {"root": key_count("/root")}
login = os.environ.get("SUDO_USER")
if login and login != "root":
    result["authorized_keys"][login] = key_count(os.path.expanduser("~" + login))

for name in options["services"]:
    result["services"][name] = (lines("systemctl", "is-active", name) or ["unknown"])[0]

code, banned = run("fail2ban-client", "status", "sshd")
found = re.search(r"Currently banned:\s*(\d+)", banned)
result["fail2ban_sshd"] = {"jail": code == 0, "currently_banned": int(found.group(1)) if found else None}

ufw = lines("ufw", "status", "verbose")
ipv6 = None
try:
    with open("/etc/default/ufw") as stream:
        ipv6 = next((l.split("=", 1)[1].strip() for l in stream if l.startswith("IPV6=")), None)
except OSError:
    pass
result["ufw"] = {"status": ufw, "ipv6": ipv6}

for line in lines("ip", "-6", "-o", "addr", "show", "scope", "global"):
    columns = line.split()
    if "inet6" in columns:
        result["ipv6_global"].append(columns[columns.index("inet6") + 1])

print(json.dumps(result))
'''

ROLES: dict[str, dict[str, Any]] = {
    "exit": {
        "xray": ["/usr/local/x-ui/bin/xray-linux-*"],
        "panel": "/usr/local/x-ui/x-ui",
        "release_marker": "/opt/proxy-pipeline/x-ui-release",
        "services": ["x-ui", "ufw", "fail2ban"],
    },
    "bridge": {
        "xray": ["/usr/local/bin/xray"],
        "services": ["xray", "nginx", "ufw", "fail2ban", "chain-probe.timer"],
    },
}


def collect(
    host: str, user: str, key: Path, role: str, runner: Any = subprocess.run
) -> dict[str, Any]:
    """Run the collector on one node. Never raises: an unreachable node is an `error` entry."""
    prefix = "" if user == "root" else "sudo -n "
    remote = f"{prefix}python3 - {shlex.quote(json.dumps(ROLES[role]))}"
    cmd = [
        "ssh",
        "-i",
        str(key),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=no",
        f"{user}@{host}",
        remote,
    ]
    started = time.time()
    try:
        proc = runner(cmd, input=COLLECTOR, capture_output=True, text=True, timeout=120)
    except Exception as e:  # noqa: BLE001 — report, do not abort the other node
        return {"role": role, "address": host, "error": str(e)}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "ssh failed").strip().splitlines()[-1:]
        return {"role": role, "address": host, "error": detail[0] if detail else "ssh failed"}
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"role": role, "address": host, "error": "collector returned no JSON"}
    data.update(role=role, address=host)
    data["clock_skew_seconds"] = round(data.pop("time") - (started + time.time()) / 2, 1)
    return data


def _xray_version(text: str) -> str:
    found = re.match(r"Xray ([0-9]+\.[0-9]+\.[0-9]+)", text or "")
    return found.group(1) if found else ""


def pin_status(node: dict[str, Any], cfg: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    """(item, installed, pinned, verdict) rows; verdict is match, MISMATCH or unpinned."""
    if node.get("error"):
        return []
    xray = node["xray"][0] if len(node.get("xray", [])) == 1 else {}
    installed_xray = _xray_version(xray.get("version", ""))
    if node["role"] == "exit":
        try:
            pin = world_release.pin_from_config(cfg)
        except ValueError:
            pin = {}
        xui = world_release.VERSION_RE.fullmatch(str(node.get("xui", {}).get("version", "")))
        rows = [
            ("3X-UI version", xui.group(1) if xui else "", pin.get("xui_version", "")),
            ("Xray version", installed_xray, pin.get("xray_version", "")),
            ("Xray SHA256", str(xray.get("sha256") or ""), pin.get("xray_sha256", "")),
        ]
    else:
        release = cfg.get("xray", {}).get("bridge_release") or {}
        pinned = str(release.get("version") or "").strip() if isinstance(release, dict) else ""
        rows = [("Xray version", installed_xray, pinned)]
    return [
        (item, got or "?", want or "—", "unpinned" if not want else "match" if got == want else "MISMATCH")
        for item, got, want in rows
    ]


def render(nodes: list[dict[str, Any]], cfg: dict[str, Any]) -> str:
    out: list[str] = []
    for node in nodes:
        out.append(f"== {node['role']} {node['address']} ==")
        if node.get("error"):
            out.append(f"  UNREACHABLE: {node['error']}")
            continue
        out.append(f"  {node['os']}  kernel {node['kernel']}  {node['arch']}  host {node['host']}")
        out.append(f"  clock skew vs controller: {node['clock_skew_seconds']:+.1f}s")
        for item in node["xray"]:
            out.append(f"  xray   {item['path']}: {item['version'] or 'no version output'}")
            out.append(f"         sha256 {item['sha256']}")
        if not node["xray"]:
            out.append("  xray   NOT FOUND")
        if "xui" in node:
            out.append(
                f"  3x-ui  {node['xui']['version'] or 'no version output'}"
                f"  (installed from pin: {node['xui']['installed_pin'] or 'unrecorded'})"
            )
        for item, got, want, verdict in pin_status(node, cfg):
            out.append(f"  pin    {item}: installed {got}  pinned {want}  -> {verdict}")
        services = "  ".join(f"{name}={state}" for name, state in node["services"].items())
        out.append(f"  services  {services}")
        sshd = "  ".join(f"{key}={value}" for key, value in sorted(node["sshd"].items()))
        out.append(f"  sshd   {sshd or 'effective settings unavailable'}")
        keys = "  ".join(f"{user}={count}" for user, count in node["authorized_keys"].items())
        out.append(f"  authorized_keys  {keys}   fail2ban sshd: {node['fail2ban_sshd']}")
        status = node["ufw"]["status"]
        out.append(f"  ufw    {status[0] if status else 'not installed'}  IPV6={node['ufw']['ipv6']}")
        out.extend(f"         {line}" for line in status[1:] if line.strip())
        out.append(f"  ipv6 global addresses: {', '.join(node['ipv6_global']) or 'none'}")
        out.append("  listeners:")
        for sock in sorted(
            node["listeners"],
            key=lambda s: (s["proto"], int(s["port"]) if s["port"].isdigit() else 0, s["address"]),
        ):
            out.append(f"    {sock['proto']:<4}{sock['address']}:{sock['port']}  {sock['process']}")
    return "\n".join(out)
