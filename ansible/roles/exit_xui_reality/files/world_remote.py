"""Small SSH-only WORLD helper. Requests and secret results travel on stdin/stdout."""

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


def require_xray_listeners(binary, ports):
    required = set(ports)
    if not required:
        return
    sockets = {}
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        if not table.exists():
            continue
        for line in table.read_text().splitlines()[1:]:
            columns = line.split()
            if columns[3] == "0A":
                sockets[columns[9]] = int(columns[1].rsplit(":", 1)[1], 16)
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            if (process / "exe").resolve() != binary.resolve():
                continue
            owned = set()
            for descriptor in (process / "fd").iterdir():
                try:
                    target = os.readlink(descriptor)
                except OSError:
                    continue
                if target.startswith("socket:["):
                    owned.add(sockets.get(target[8:-1]))
            if required <= owned:
                return
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    raise ValueError("expected WORLD listeners are not owned by the selected Xray binary")


def describe(binary):
    version = subprocess.run(
        [str(binary), "version"], capture_output=True, text=True, timeout=15, check=True,
    ).stdout.splitlines()[0]
    digest = hashlib.sha256()
    with binary.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"version": version, "sha256": digest.hexdigest()}


def execute(request):
    binary = Path(request["binary"])
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("WORLD Xray binary is unavailable")
    env = {**os.environ, "XRAY_LOCATION_ASSET": str(binary.parent)}
    action = request["action"]
    if action == "release":
        # Versions and checksums only: nothing here is secret, and nothing is changed.
        panel = subprocess.run(
            [request["panel_binary"], "-v"], capture_output=True, text=True, timeout=15, check=True,
        ).stdout.strip().splitlines()
        return {"xui": panel[-1].strip() if panel else "", "xray": describe(binary)}
    if action == "bootstrap":
        database = Path(request["database"]).resolve()
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as source:
            if source.execute("SELECT count(*) FROM inbounds").fetchone()[0] != 0:
                raise ValueError("refusing to bootstrap a nonempty panel")
            if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("invalid panel database")
            fd, backup = tempfile.mkstemp(prefix="pipeline-bootstrap-", suffix=".db", dir=database.parent)
            os.close(fd)
            with sqlite3.connect(backup) as target:
                source.backup(target)
        return {"backup": backup}
    if action == "keys":
        args = [str(binary), "x25519"]
        if request.get("private_key"):
            args += ["-i", request["private_key"]]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=15, env=env)
        if proc.returncode:
            raise ValueError("WORLD x25519 failed")
        return {"output": proc.stdout}
    if action == "validate":
        with tempfile.TemporaryDirectory(prefix=".pipeline-", dir=binary.parent) as scratch:
            candidate = Path(scratch) / "candidate.json"
            candidate.write_text(json.dumps(request["config"]))
            candidate.chmod(0o600)
            proc = subprocess.run(
                [str(binary), "run", "-test", "-config", str(candidate)],
                cwd=binary.parent, env=env, capture_output=True, timeout=30,
            )
            if proc.returncode:
                raise ValueError("WORLD Xray rejected the full candidate before API changes")
        return describe(binary)
    if action == "ports":
        require_xray_listeners(binary, [int(p) for p in request["ports"]])
        for port in request["ports"]:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=3):
                pass
        return {"ok": True}
    raise ValueError("unknown WORLD helper action")


if __name__ == "__main__":
    try:
        print(json.dumps(execute(json.load(sys.stdin))))
    except Exception:
        # Xray diagnostics can contain credentials; do not forward raw errors.
        print("WORLD helper failed; inspect the requested validation stage", file=sys.stderr)
        sys.exit(1)
