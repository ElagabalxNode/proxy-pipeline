"""Pre-change backup of controller state and node configuration, with integrity verification.

One directory per backup under <repo>/backups (mode 0700, files 0600): a tar per source plus a
manifest of checksums. Archives hold credentials and keys, so they never leave the controller
and are not unpacked here. Nodes are only read; the panel database is copied with SQLite's
online backup, so a running 3X-UI yields a consistent file.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

REMOTE = r'''
import glob, io, json, os, sqlite3, sys, tarfile, tempfile

options = json.loads(sys.argv[1])
missing = []
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as out:
    for pattern in options["files"]:
        paths = [p for p in sorted(glob.glob(pattern)) if os.path.isfile(p)]
        if not paths:
            missing.append(pattern)
        for path in paths:
            out.add(path, arcname=path.lstrip("/"), recursive=False)
    for database in options.get("sqlite", []):
        if not os.path.isfile(database):
            missing.append(database)
            continue
        with tempfile.TemporaryDirectory() as scratch:
            copy = os.path.join(scratch, "copy.db")
            source = sqlite3.connect("file:" + database + "?mode=ro", uri=True)
            target = sqlite3.connect(copy)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            out.add(copy, arcname=database.lstrip("/"))
    note = json.dumps(missing).encode()
    info = tarfile.TarInfo("MISSING.json")
    info.size = len(note)
    out.addfile(info, io.BytesIO(note))
'''

SSH_FILES = ["/etc/ssh/sshd_config", "/etc/ssh/sshd_config.d/*", "/etc/fail2ban/jail.local"]
UFW_FILES = ["/etc/ufw/user.rules", "/etc/ufw/user6.rules", "/etc/ufw/ufw.conf", "/etc/default/ufw"]
NODES: dict[str, dict[str, list[str]]] = {
    "exit": {
        "files": [
            "/usr/local/x-ui/bin/config.json", "/etc/systemd/system/x-ui.service",
            "/opt/proxy-pipeline/x-ui-release", *SSH_FILES, *UFW_FILES,
        ],
        "sqlite": ["/etc/x-ui/x-ui.db"],
    },
    "bridge": {
        "files": [
            "/usr/local/etc/xray/config.json", "/etc/systemd/system/xray.service",
            "/etc/systemd/system/xray.service.d/*", "/etc/nginx/nginx.conf",
            "/etc/nginx/conf.d/*", "/etc/nginx/sites-enabled/*", *SSH_FILES, *UFW_FILES,
        ],
    },
}
# Controller files, relative to the repository root. Probe logs and cached artifacts are bulky
# and reproducible; locks are meaningless outside the running process.
LOCAL = ["config/pipeline.yaml", "terraform/terraform.tfvars", "terraform/terraform.tfstate",
         "terraform/terraform.tfstate.backup", ".client-links", ".env"]
STATE_SKIP = {"probe", "artifacts", "watchdog"}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def _members(data: bytes) -> dict[str, str]:
    """SHA256 of every regular member; raises tarfile.TarError on a damaged archive."""
    found: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for member in archive:
            if member.isfile():
                found[member.name] = _sha256(archive.extractfile(member).read())
    return found


def local_archive(root: Path, state_dir: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in LOCAL:
            if (root / name).is_file():
                archive.add(root / name, arcname=name, recursive=False)
        for path in sorted(state_dir.rglob("*")):
            relative = path.relative_to(state_dir)
            if relative.parts[0] in STATE_SKIP or relative.name.endswith(".lock"):
                continue
            if path.is_file() and not path.is_symlink():
                archive.add(path, arcname=str(Path("state") / relative), recursive=False)
    return buffer.getvalue()


def node_archive(host: str, user: str, key: Path, role: str, runner: Any = subprocess.run) -> bytes:
    prefix = "" if user == "root" else "sudo -n "
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
        f"{prefix}python3 - {shlex.quote(json.dumps(NODES[role]))}",
    ]
    proc = runner(cmd, input=REMOTE.encode(), capture_output=True, timeout=300)
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode(errors="replace").strip().splitlines()[-1:]
        raise OSError(detail[0] if detail else "ssh failed")
    return proc.stdout


def git_revision(root: Path) -> dict[str, Any]:
    def git(*args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    return {"sha": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def create(
    root: Path,
    state_dir: Path,
    nodes: list[tuple[str, str, Path, str]],
    runner: Any = subprocess.run,
    now: float | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Write one backup directory. A node that cannot be read is recorded, not fatal."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    parent = root / "backups"
    parent.mkdir(mode=0o700, exist_ok=True)
    target = parent / stamp
    target.mkdir(mode=0o700)  # an existing directory is never reused
    manifest: dict[str, Any] = {
        "created": stamp, "git": git_revision(root), "archives": {}, "failed": {}, "missing": {},
    }
    sources = [("local", lambda: local_archive(root, state_dir))] + [
        (role, lambda host=host, user=user, key=key, role=role: node_archive(
            host, user, key, role, runner))
        for host, user, key, role in nodes
    ]
    for name, read in sources:
        try:
            data = read()
            members = _members(data)
        except (OSError, tarfile.TarError, subprocess.SubprocessError) as e:
            manifest["failed"][name] = str(e)
            continue
        _write(target / f"{name}.tar", data)
        if "MISSING.json" in members:
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                manifest["missing"][name] = json.load(archive.extractfile("MISSING.json"))
        manifest["archives"][name] = {"sha256": _sha256(data), "size": len(data), "members": members}
    for host, _, _, role in nodes:
        manifest.setdefault("nodes", {})[role] = host
    _write(target / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
    return target, manifest


def verify(directory: Path) -> list[str]:
    """Re-read a backup from disk; returns problems (empty = every recorded byte is intact)."""
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        archives = manifest["archives"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return [f"manifest unreadable: {e}"]
    problems = [f"{name}: not backed up ({why})" for name, why in manifest.get("failed", {}).items()]
    for name, recorded in archives.items():
        try:
            data = (directory / f"{name}.tar").read_bytes()
            members = _members(data)
        except (OSError, tarfile.TarError) as e:
            problems.append(f"{name}.tar unreadable: {e}")
            continue
        if _sha256(data) != recorded["sha256"] or members != recorded["members"]:
            problems.append(f"{name}.tar differs from the manifest")
            continue
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            for member in members:
                content = archive.extractfile(member).read()
                if member.endswith(".db"):
                    problems += [f"{name}:{member}: {p}" for p in _check_sqlite(content)]
                elif Path(member).name.startswith("."):
                    continue  # scratch/journal dotfiles: the checksum is the whole claim
                elif member.endswith(".json") or member.endswith(".tfstate"):
                    try:
                        json.loads(content)
                    except ValueError:
                        problems.append(f"{name}:{member}: not valid JSON")
    return problems


def _check_sqlite(content: bytes) -> list[str]:
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "copy.db"
        _write(path, content)
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error as e:
            return [f"not a readable SQLite database: {e}"]
    return [] if result == "ok" else [f"integrity_check: {result}"]
