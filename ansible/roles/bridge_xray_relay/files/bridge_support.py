"""Standard-library helpers run by Ansible; never print secrets in error messages."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import UUID
from zipfile import ZipFile


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare_archive(archive: Path, checksum: str, version: str, cache: Path) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("archive SHA256 must be 64 lowercase hex characters")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("a pinned numeric Xray version is required")
    if file_hash(archive) != checksum:
        raise ValueError("Xray archive SHA256 mismatch; nothing installed")
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / f"{version}-{checksum}"
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=cache) as scratch:
        stage = Path(scratch)
        hashes = {}
        with ZipFile(archive) as bundle:
            for name in ("xray", "geoip.dat", "geosite.dat"):
                matches = [i for i in bundle.infolist() if i.filename == name]
                if len(matches) != 1 or stat.S_IFMT(matches[0].external_attr >> 16) not in (0, stat.S_IFREG):
                    raise ValueError(f"archive must contain one regular {name}")
                # Extract only these exact names; archive paths are never trusted.
                target = stage / name
                with bundle.open(matches[0]) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if name == "xray" else 0o644)
                hashes[name] = file_hash(target)
        proc = subprocess.run(
            [str(stage / "xray"), "version"], capture_output=True, text=True, timeout=10,
        )
        if proc.returncode or not re.match(rf"^Xray {re.escape(version)}(?:\s|$)", proc.stdout):
            raise ValueError("archive binary does not run or does not match the pinned version")
        changed = not destination.exists()
        if changed:
            # All files and the binary version are verified before the directory becomes visible.
            published = stage / "bundle"
            published.mkdir(mode=0o755)
            for name in hashes:
                (stage / name).rename(published / name)
            published.rename(destination)
        else:
            if destination.is_symlink() or any(
                (destination / name).is_symlink() or file_hash(destination / name) != digest
                for name, digest in hashes.items()
            ):
                raise ValueError("verified Xray cache was modified; refusing to reuse it")
    return {"directory": str(destination), "version": version, "hashes": hashes, "changed": changed}


def _key(value: str, field: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        raise ValueError(f"invalid {field}")
    if len(base64.b64decode(value + "=", altchars=b"-_", validate=True)) != 32:
        raise ValueError(f"invalid {field}")


def validate_identity(identity: dict) -> dict:
    if not isinstance(identity, dict):
        raise ValueError("identity must be a JSON object")
    try:
        UUID(identity["uuid"])
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ValueError("identity has invalid uuid") from exc
    _key(identity.get("private_key", ""), "private_key")
    if "public_key" in identity:
        _key(identity["public_key"], "public_key")
    sid = identity.get("short_id", "")
    if not isinstance(sid, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,8}", sid):
        raise ValueError("identity has invalid short_id")
    for field in ("sni", "dest"):
        value = identity.get(field, "")
        if not isinstance(value, str) or not value or any(c.isspace() for c in value):
            raise ValueError(f"identity has invalid {field}")
    sizes = {"2022-blake3-aes-128-gcm": 16, "2022-blake3-aes-256-gcm": 32,
             "2022-blake3-chacha20-poly1305": 32}
    method = identity.get("ss_method")
    size = sizes.get(method) if isinstance(method, str) else None
    try:
        password = base64.b64decode(identity["ss_password"], validate=True)
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("identity has invalid ss_password") from exc
    # A key longer than the method needs is what an old generator produced (18 bytes for a
    # 16-byte method). Xray serves such an inbound, and a client link already carries it, so a
    # LIVE config keeps it: replacing it here would silently rotate a client credential. Only
    # read_identity("live") sets the marker; new identities and backups stay exact.
    legacy = identity.get("ss_key_legacy") is True and size is not None and len(password) > size
    if size is None or (len(password) != size and not legacy):
        raise ValueError("identity has invalid SS2022 method/key length")
    if "xhttp_uuid" in identity or "xhttp_path" in identity:
        try:
            UUID(identity["xhttp_uuid"])
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise ValueError("identity has invalid xhttp_uuid") from exc
        path = identity.get("xhttp_path")
        if not isinstance(path, str) or not re.fullmatch(r"/[A-Za-z0-9_-]{8,64}", path):
            raise ValueError("identity has invalid xhttp_path")
        if identity["xhttp_uuid"] == identity["uuid"]:
            raise ValueError("XHTTP and REALITY entries must not share a client UUID")
    return identity


def read_identity(path: Path, kind: str) -> dict:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {kind} identity; restore the file before configuring") from exc
    if kind == "backup":
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if k != "ss_key_legacy"}
        return validate_identity(data)
    if data == {}:  # Empty bootstrap config from the official installer, not a saved identity.
        return {}
    try:
        vless = [i for i in data["inbounds"] if i.get("tag") == "vless-reality-in"]
        ss = [i for i in data["inbounds"] if i.get("tag") == "ss2022-in"]
        if len(vless) != 1 or len(ss) != 1:
            raise ValueError("missing or duplicate managed inbound")
        ib, ss = vless[0], ss[0]
        clients = ib["settings"]["clients"]
        rs = ib["streamSettings"]["realitySettings"]
        if (ib["protocol"] != "vless" or ss["protocol"] != "shadowsocks"
                or len(clients) != 1 or len(rs["shortIds"]) != 1 or len(rs["serverNames"]) != 1):
            raise ValueError("unsupported managed inbound layout")
        found = {
            "uuid": clients[0]["id"], "private_key": rs["privateKey"],
            "short_id": rs["shortIds"][0], "sni": rs["serverNames"][0],
            "dest": rs["dest"], "ss_password": ss["settings"]["password"],
            "ss_method": ss["settings"]["method"],
        }
        xhttp = [i for i in data["inbounds"] if i.get("tag") == "vless-xhttp-in"]
        if xhttp:
            entry = xhttp[0]
            stream, users = entry["streamSettings"], entry["settings"]["clients"]
            if (len(xhttp) != 1 or entry["protocol"] != "vless" or len(users) != 1
                    or users[0].get("flow", "") != "" or entry.get("listen") != "127.0.0.1"
                    or stream["network"] != "xhttp" or stream.get("security", "none") != "none"):
                raise ValueError("unsupported XHTTP entry layout")
            found.update(xhttp_uuid=users[0]["id"], xhttp_path=stream["xhttpSettings"]["path"])
        try:
            return validate_identity(found)
        except ValueError as exc:
            if "SS2022 method/key length" not in str(exc):
                raise
            return validate_identity({**found, "ss_key_legacy": True})
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
        raise ValueError("live config has invalid/unsupported identity; refusing key rotation") from exc


def parse_x25519(output: str) -> dict:
    def one(pattern: str, field: str) -> str:
        matches = re.findall(pattern, output, re.MULTILINE | re.IGNORECASE)
        if len(matches) != 1:
            raise ValueError(f"x25519 output has missing/ambiguous {field}")
        _key(matches[0], field)
        return matches[0]

    return {
        "private_key": one(r"^Private(?:\s*Key)?\s*:\s*(\S+)\s*$", "private_key"),
        "public_key": one(
            r"^(?:Public(?:\s*Key)?|Password(?:\s*\(PublicKey\))?)\s*:\s*(\S+)\s*$",
            "public_key",
        ),
    }


def recover_keys(output: str, identity: dict) -> dict:
    keys = parse_x25519(output)
    if not identity:
        return keys
    validate_identity(identity)

    def clamped(key: str) -> bytes:
        # Scalar decoding: https://www.rfc-editor.org/rfc/rfc7748.html#section-5
        value = bytearray(base64.urlsafe_b64decode(key + "="))
        value[0] &= 248
        value[31] = (value[31] & 127) | 64
        return bytes(value)

    # Xray may print the clamped scalar even for an equivalent saved private key.
    # Keep the saved encoding while checking that the derived pair belongs to it.
    if clamped(identity["private_key"]) != clamped(keys["private_key"]):
        raise ValueError("derived private key does not match the saved identity")
    if identity.get("public_key", keys["public_key"]) != keys["public_key"]:
        raise ValueError("derived public key does not match the saved identity")
    return {**keys, "private_key": identity["private_key"]}


def restore_file(request: dict) -> dict:
    """Put saved bytes back exactly; Ansible's copy/content would reformat JSON."""
    path = Path(request["path"])
    content = base64.b64decode(request["content"], validate=True)
    fd, temp = tempfile.mkstemp(prefix=".restore-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), int(str(request["mode"]), 8))
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return {"restored": path.name}


def check_https(request: dict) -> dict:
    """What a client and what an address scanner see on the HTTPS entry."""
    name, port = request["name"], int(request["port"])
    address = (request.get("host") or "127.0.0.1", port)
    trusted = ssl.create_default_context(cafile=request.get("ca_file") or None)
    try:
        with socket.create_connection(address, timeout=10) as raw, \
                trusted.wrap_socket(raw, server_hostname=name) as tls:
            served = tls.getpeercert(binary_form=True)
            tls.sendall(f"GET / HTTP/1.1\r\nHost: {name}\r\nConnection: close\r\n\r\n".encode())
            status = tls.recv(64).split(b"\r\n", 1)[0]
    except (OSError, ssl.SSLError) as exc:
        raise ValueError("HTTPS entry failed TLS verification for its own name") from exc
    if b" 200" not in status:
        raise ValueError("HTTPS entry did not serve its page")
    blind = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    blind.check_hostname = False
    blind.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection(address, timeout=10) as raw, \
                blind.wrap_socket(raw, server_hostname="unknown.invalid") as tls:
            shown = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError):
        shown = None  # a refused handshake reveals nothing either
    if shown == served:
        raise ValueError("requests for unknown names are shown the bridge certificate")
    return {"https": "ok"}


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    prepare = sub.add_parser("prepare")
    for name in ("archive", "checksum", "version", "cache"):
        prepare.add_argument(f"--{name}", required=True)
    read = sub.add_parser("read")
    read.add_argument("kind", choices=["live", "backup"])
    read.add_argument("path")
    sub.add_parser("parse-keys")
    sub.add_parser("recover-keys")
    sub.add_parser("restore-file")
    sub.add_parser("check-https")
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            result = prepare_archive(Path(args.archive), args.checksum, args.version, Path(args.cache))
        elif args.action == "read":
            result = read_identity(Path(args.path), args.kind)
        elif args.action == "check-https":
            result = check_https(json.loads(sys.stdin.read()))
        elif args.action == "restore-file":
            result = restore_file(json.loads(sys.stdin.read()))
        elif args.action == "recover-keys":
            data = json.loads(sys.stdin.read())
            result = recover_keys(data["output"], data["identity"])
        else:
            result = parse_x25519(sys.stdin.read())
        print(json.dumps(result))
    except Exception as exc:
        # Never echo JSON, key material, subprocess output or arbitrary file contents.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(f"bridge preparation failed: {message}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
