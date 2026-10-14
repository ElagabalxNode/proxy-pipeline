"""Pinned WORLD release: 3X-UI archive installed by Terraform, Xray binary checked by configure.

Two places carry the pin because two tools consume it: terraform.tfvars installs the archive
(xui_version + xui_archive_sha256), config/pipeline.yaml states what configure must find on the
node (xray.world_release). This module keeps them consistent and can prove, on the controller
and before an old Exit is destroyed, that the pinned archive really contains the pinned Xray.
"""

from __future__ import annotations

import hashlib
import os
import re
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pipelinectl import cherry

VERSION_RE = re.compile(r"v?([0-9]+\.[0-9]+\.[0-9]+)")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
ARCHIVE_URL = "https://github.com/MHSanaei/3x-ui/releases/download/v{version}/x-ui-linux-{arch}.tar.gz"
Fetch = Callable[[str, Path], None]


def _version(value: Any, name: str) -> str:
    match = VERSION_RE.fullmatch(str(value or "").strip())
    if not match:
        raise ValueError(f"set {name} to the approved version (x.y.z); 'latest' is not a pin")
    return match.group(1)


def _sha256(value: Any, name: str) -> str:
    text = str(value or "").strip().lower()
    if not SHA256_RE.fullmatch(text):
        raise ValueError(f"set {name} to a SHA256 (64 hex characters)")
    return text


def pin_from_config(cfg: dict[str, Any]) -> dict[str, str]:
    """What configure must find installed on WORLD; raises ValueError when not fully pinned."""
    release = cfg.get("xray", {}).get("world_release")
    if not isinstance(release, dict):
        raise ValueError("xray.world_release must contain xui_version, xray_version and xray_sha256")
    return {
        "xui_version": _version(release.get("xui_version"), "xray.world_release.xui_version"),
        "xray_version": _version(release.get("xray_version"), "xray.world_release.xray_version"),
        "xray_sha256": _sha256(release.get("xray_sha256"), "xray.world_release.xray_sha256"),
    }


def terraform_pin(cfg: dict[str, Any]) -> dict[str, str]:
    """What Terraform installs on a new Exit, from terraform.tfvars."""
    tfvars = cherry.tfvars(cfg)
    return {
        "xui_version": _version(tfvars.get("xui_version"), "xui_version in terraform.tfvars"),
        "archive_sha256": _sha256(
            tfvars.get("xui_archive_sha256"), "xui_archive_sha256 in terraform.tfvars"
        ),
    }


def require_same_version(pin: dict[str, str], tf_pin: dict[str, str]) -> None:
    if pin["xui_version"] != tf_pin["xui_version"]:
        raise ValueError(
            f"3X-UI pin differs: terraform.tfvars installs {tf_pin['xui_version']},"
            f" xray.world_release expects {pin['xui_version']}"
        )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_archive(path: Path) -> dict[str, str]:
    """Checksums of a 3X-UI archive and of its single bundled Xray, without unpacking to disk."""
    archive_sha256 = file_sha256(path)
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = [
                m for m in archive.getmembers()
                if m.isfile() and re.fullmatch(r"x-ui/bin/xray-linux-[A-Za-z0-9_-]+", m.name)
            ]
            if len(members) != 1:
                raise ValueError("archive does not contain exactly one x-ui/bin/xray-linux-* binary")
            inner = hashlib.sha256()
            source = archive.extractfile(members[0])
            for block in iter(lambda: source.read(1024 * 1024), b""):
                inner.update(block)
    except (tarfile.TarError, OSError, EOFError) as e:
        raise ValueError(f"{path.name} is not a readable 3X-UI archive: {e}") from e
    return {
        "archive_sha256": archive_sha256,
        "xray_member": members[0].name,
        "xray_sha256": inner.hexdigest(),
    }


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".download-", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
            for block in iter(lambda: response.read(1024 * 1024), b""):
                out.write(block)
        os.replace(temp, dest)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def verify(
    cfg: dict[str, Any],
    cache_dir: Path,
    arch: str = "amd64",
    archive: Path | None = None,
    fetch: Fetch = download,
) -> dict[str, str]:
    """Prove the Terraform archive pin and the configure Xray pin describe one artifact.

    The archive is taken from `archive`, else from the cache, else downloaded. A cached file
    whose checksum no longer matches the pin is downloaded again rather than trusted.
    """
    pin, tf_pin = pin_from_config(cfg), terraform_pin(cfg)
    require_same_version(pin, tf_pin)
    path = archive or cache_dir / f"x-ui-{tf_pin['xui_version']}-linux-{arch}.tar.gz"
    if archive is None:
        if not (path.is_file() and file_sha256(path) == tf_pin["archive_sha256"]):
            fetch(ARCHIVE_URL.format(version=tf_pin["xui_version"], arch=arch), path)
    found = inspect_archive(path)
    if found["archive_sha256"] != tf_pin["archive_sha256"]:
        raise ValueError(
            f"{path.name}: SHA256 {found['archive_sha256']} differs from xui_archive_sha256"
            f" {tf_pin['archive_sha256']}; a new Exit would refuse to install it"
        )
    if found["xray_sha256"] != pin["xray_sha256"]:
        raise ValueError(
            f"{path.name}: bundled Xray SHA256 {found['xray_sha256']} differs from"
            f" xray.world_release.xray_sha256 {pin['xray_sha256']}; configure would refuse the new Exit"
        )
    return {**found, "archive": str(path), "xui_version": tf_pin["xui_version"]}
