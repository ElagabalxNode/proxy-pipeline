"""Exercise the exact helper shipped to RU, including rejection before key rotation."""

import base64
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from test_bridge_config import render_bridge

HELPER = Path(__file__).resolve().parents[2] / (
    "ansible/roles/bridge_xray_relay/files/bridge_support.py"
)
spec = importlib.util.spec_from_file_location("bridge_support", HELPER)
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
PRIVATE = base64.urlsafe_b64encode(b"\x01" * 32).decode().rstrip("=")
PUBLIC = base64.urlsafe_b64encode(b"\x02" * 32).decode().rstrip("=")


@pytest.fixture
def identity(tmp_path):
    path = tmp_path / "live.json"
    path.write_text(json.dumps(render_bridge("xhttp")))
    return support.read_identity(path, "live")


def archive_at(path, version="25.10.15", *, symlink=False, missing=False):
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        entry = ZipInfo("xray")
        entry.external_attr = (stat.S_IFLNK if symlink else stat.S_IFREG) << 16
        archive.writestr(entry, f"#!/bin/sh\nprintf 'Xray {version} (test fixture)\\n'\n")
        archive.writestr("geoip.dat", b"test data")
        if not missing:
            archive.writestr("geosite.dat", b"test data")
        archive.writestr("../unexpected-file", b"must never be extracted")
    return support.file_hash(path)


def test_archive_verified_and_idempotent(tmp_path):
    archive, cache = tmp_path / "release.zip", tmp_path / "cache"
    checksum = archive_at(archive)
    first = support.prepare_archive(archive, checksum, "25.10.15", cache)
    assert first["changed"]
    bundle = Path(first["directory"])
    assert {p.name for p in bundle.iterdir()} == {"xray", "geoip.dat", "geosite.dat"}
    assert not list(tmp_path.rglob("unexpected-file"))
    assert bundle.stat().st_mode & 0o777 == 0o755
    assert (bundle / "xray").stat().st_mode & 0o777 == 0o755
    assert support.prepare_archive(archive, checksum, "25.10.15", cache) == {
        **first, "changed": False,
    }
    (bundle / "xray").write_text("tampered binary")
    with pytest.raises(ValueError, match="cache was modified"):
        support.prepare_archive(archive, checksum, "25.10.15", cache)


@pytest.mark.parametrize("failure", ["hash", "version", "symlink", "missing"])
def test_invalid_artifact_never_published(tmp_path, failure):
    archive, cache = tmp_path / "release.zip", tmp_path / "cache"
    checksum = archive_at(
        archive, symlink=failure == "symlink", missing=failure == "missing",
    )
    with pytest.raises(ValueError):
        support.prepare_archive(
            archive, "0" * 64 if failure == "hash" else checksum,
            "1.2.3" if failure == "version" else "25.10.15", cache,
        )
    assert not cache.exists() or list(cache.iterdir()) == []


@pytest.mark.parametrize("kind", ["live", "backup"])
def test_missing_is_the_only_silent_identity_fallback(tmp_path, kind):
    path = tmp_path / "missing.json"
    assert support.read_identity(path, kind) == {}
    for content in ["", "{", "null", "[]", '{"private_key": "secret"}']:
        path.write_text(content)
        with pytest.raises(ValueError):
            support.read_identity(path, kind)
    path.write_text("{}")
    if kind == "live":
        assert support.read_identity(path, kind) == {}  # official installer bootstrap
    else:
        with pytest.raises(ValueError):
            support.read_identity(path, kind)


def test_valid_live_and_backup_identity_preserved(tmp_path, identity):
    assert identity["private_key"] == PRIVATE
    path = tmp_path / "backup.json"
    saved = {**identity, "public_key": PUBLIC, "bridge_ip": "192.0.2.1"}
    path.write_text(json.dumps(saved))
    assert support.read_identity(path, "backup") == saved


@pytest.mark.parametrize("field,value", [
    ("uuid", "bad"), ("private_key", "secret"), ("public_key", "secret"),
    ("short_id", "123"), ("sni", ""), ("dest", "white space:443"),
    ("ss_method", "aes-128-gcm"), ("ss_method", []), ("ss_password", "not-base64"),
    ("ss_password", base64.b64encode(b"\x01" * 32).decode()),
])
def test_partial_or_invalid_backup_rejected(identity, field, value):
    with pytest.raises(ValueError):
        support.validate_identity({**identity, field: value})


@pytest.mark.parametrize("change", ["multi_client", "missing_ss", "foreign_tag", "duplicate"])
def test_live_layout_cannot_silently_drop_identity(tmp_path, change):
    data = render_bridge("xhttp")
    if change == "multi_client":
        data["inbounds"][0]["settings"]["clients"].append({"id": "new-user"})
    elif change == "missing_ss":
        data["inbounds"] = [i for i in data["inbounds"] if i["protocol"] != "shadowsocks"]
    elif change == "foreign_tag":
        data["inbounds"][0]["tag"] = "unmanaged"
    else:
        data["inbounds"].append(data["inbounds"][0])
    path = tmp_path / "live.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="refusing key rotation"):
        support.read_identity(path, "live")


@pytest.mark.parametrize("private,public", [
    ("Private key", "Public key"), ("PrivateKey", "Password"),
    ("PrivateKey", "Password (PublicKey)"),
])
def test_x25519_output_formats(private, public):
    output = f"{private}: {PRIVATE}\n{public}: {PUBLIC}\nHash32: ignored\n"
    assert support.parse_x25519(output) == {"private_key": PRIVATE, "public_key": PUBLIC}


@pytest.mark.parametrize("output", ["", "Private key: secret\n", f"PrivateKey: {PRIVATE}\n",
                                   f"PrivateKey: {PRIVATE}\nPassword: {PUBLIC}\nPublic key: {PUBLIC}\n"])
def test_unknown_or_ambiguous_keys_fail(output):
    with pytest.raises(ValueError):
        support.parse_x25519(output)


def test_helper_failure_does_not_echo_secret(tmp_path):
    path = tmp_path / "identity.json"
    path.write_text('{"private_key":"secret-do-not-log"')
    result = subprocess.run(
        [sys.executable, str(HELPER), "read", "backup", str(path)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "secret-do-not-log" not in result.stdout + result.stderr


def test_official_archive_and_modern_x25519(tmp_path, identity):
    raw = os.environ.get("XRAY_TEST_BINARY")
    if not raw:
        pytest.skip("set XRAY_TEST_BINARY for the official artifact regression")
    binary = Path(raw).resolve()
    archive = binary.parent / "Xray-linux-64.zip"
    assert archive.is_file(), "keep the checked official ZIP next to XRAY_TEST_BINARY"
    prepared = support.prepare_archive(
        archive, "df22ad60c1251c9fb63d7f85b3677872edf61c6715eba64b06adbfec658f4938",
        "25.10.15", tmp_path / "cache",
    )
    assert support.file_hash(binary) == prepared["hashes"]["xray"]
    proc = subprocess.run(
        [str(binary), "x25519", "-i", PRIVATE], capture_output=True, text=True, check=True,
    )
    keys = support.recover_keys(proc.stdout, identity)
    assert keys["private_key"] == PRIVATE
    assert len(keys["public_key"]) == 43
    with pytest.raises(ValueError, match="public key does not match"):
        support.recover_keys(proc.stdout, {**identity, "public_key": PUBLIC})
    with pytest.raises(ValueError, match="private key does not match"):
        support.recover_keys(proc.stdout, {**identity, "private_key": PUBLIC})


def test_live_config_keeps_an_overlong_ss2022_key_but_new_and_backup_identities_do_not(tmp_path):
    """RU found 2026-10-13: an 18-byte key from an old generator, served by Xray and in a client link."""
    import base64
    import json

    from test_bridge_xhttp_entry import render

    long_key = base64.b64encode(bytes(range(18))).decode()
    cfg = render(bridge_ss_password=long_key)
    live = tmp_path / "config.json"
    live.write_text(json.dumps(cfg))
    found = support.read_identity(live, "live")
    assert found["ss_password"] == long_key and found["ss_key_legacy"] is True
    # The marker lets the same identity through key recovery, with its key untouched.
    assert support.validate_identity(found)["ss_password"] == long_key
    # An exact-length key carries no marker; a shorter or non-base64 key is never accepted.
    live.write_text(json.dumps(render()))
    assert "ss_key_legacy" not in support.read_identity(live, "live")
    for bad in (base64.b64encode(bytes(range(12))).decode(), "not base64!"):
        live.write_text(json.dumps(render(bridge_ss_password=bad)))
        with pytest.raises(ValueError, match="refusing key rotation"):
            support.read_identity(live, "live")
    # A backup cannot use the marker to smuggle in a wrong key, and fresh identities stay exact.
    backup = tmp_path / "bridge_identity.json"
    plain = {k: v for k, v in found.items() if k != "ss_key_legacy"}
    for content in (plain, found):
        backup.write_text(json.dumps(content))
        with pytest.raises(ValueError, match="SS2022 method/key length"):
            support.read_identity(backup, "backup")
    with pytest.raises(ValueError, match="SS2022 method/key length"):
        support.validate_identity(plain)
