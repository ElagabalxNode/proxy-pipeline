"""Certificate for the bridge's HTTPS entry, issued and renewed on the controller.

A certificate for the bridge zone (the wildcard and the zone itself, so a site on the bare
domain needs no reissue) is obtained with the DNS-01 challenge by a pinned
lego binary. Nothing runs on RU and no port is opened there: RU's own egress is unreliable, and
a wildcard keeps the bridge host name out of the public certificate logs. The result is
published as state/certs/<zone>/{fullchain,privkey}.pem, which `configure` delivers to RU.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from pipelinectl import timeweb_dns, world_release

ARCHIVE_URL = "https://github.com/go-acme/lego/releases/download/v{version}/lego_v{version}_linux_{arch}.tar.gz"
ZONE_RE = re.compile(r"([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
PROVIDERS = {"timewebcloud": "TIMEWEBCLOUD_AUTH_TOKEN"}


def zone_of(cfg: dict[str, Any]) -> str:
    """The DNS zone the bridge host lives in: bridge_entry.zone, else the host minus its first label."""
    entry = cfg.get("bridge_entry") or {}
    zone = str(entry.get("zone") or "").strip().lower()
    if not zone:
        zone = str(entry.get("domain") or "").strip().lower().partition(".")[2]
    if not ZONE_RE.fullmatch(zone):
        raise ValueError("set bridge_entry.zone (or bridge_entry.domain) to derive the certificate zone")
    return zone


def published(state_dir: Path, zone: str, staging: bool = False) -> tuple[Path, Path]:
    directory = state_dir / "certs" / (zone + ("-staging" if staging else ""))
    return directory / "fullchain.pem", directory / "privkey.pem"


def lego_binary(cfg: dict[str, Any], cache_dir: Path, fetch: world_release.Fetch | None = None) -> Path:
    """The pinned ACME client: downloaded once, checksum-verified, never 'latest'."""
    fetch = fetch or world_release.download
    release = (cfg.get("acme") or {}).get("lego_release") or {}
    version = str(release.get("version") or "").strip().lstrip("v")
    checksum = str(release.get("archive_sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("set acme.lego_release.version and archive_sha256 (official linux tar.gz)")
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if not arch:
        raise ValueError(f"no pinned lego build for {platform.machine()}")
    binary = cache_dir / f"lego-{version}-{arch}" / "lego"
    stamp = binary.with_name("archive.sha256")
    if binary.is_file() and stamp.is_file() and stamp.read_text().strip() == checksum:
        return binary
    archive = cache_dir / f"lego_v{version}_linux_{arch}.tar.gz"
    if not (archive.is_file() and world_release.file_sha256(archive) == checksum):
        fetch(ARCHIVE_URL.format(version=version, arch=arch), archive)
    if world_release.file_sha256(archive) != checksum:
        raise ValueError(f"{archive.name}: SHA256 differs from acme.lego_release.archive_sha256")
    binary.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        member = tar.getmember("lego")
        if not member.isfile():
            raise ValueError(f"{archive.name} does not contain the lego binary")
        with tar.extractfile(member) as source, binary.open("wb") as target:
            shutil.copyfileobj(source, target)
    binary.chmod(0o755)
    stamp.write_text(checksum + "\n")
    return binary


def _publish(source: Path, target: Path) -> bool:
    data = source.read_bytes()
    if target.is_file() and target.read_bytes() == data:
        return False
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".cert-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), 0o600)
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return True


def issue(
    cfg: dict[str, Any],
    state_dir: Path,
    zone: str,
    staging: bool = False,
    force: bool = False,
    runner: Any = subprocess.run,
    fetch: world_release.Fetch | None = None,
) -> dict[str, Any]:
    """Obtain the wildcard certificate, or renew it when it is due; publish it if it changed.

    `lego run` does both: with a stored certificate it only renews near expiry (or with force).
    Staging uses Let's Encrypt's test CA and a separate directory, so a rehearsal never touches
    the certificate RU serves and does not count against production rate limits.
    """
    if not ZONE_RE.fullmatch(zone):
        raise ValueError(f"not a DNS zone: {zone!r}")
    acme = cfg.get("acme") or {}
    provider = str(acme.get("dns_provider") or "timewebcloud")
    if provider not in PROVIDERS:
        raise ValueError(f"acme.dns_provider {provider!r} is not supported here")
    auth = timeweb_dns.token(cfg)
    if not auth:
        raise ValueError("no DNS API token: set TIMEWEBCLOUD_AUTH_TOKEN (or TWC_TOKEN) in the environment or .env")
    binary = lego_binary(cfg, state_dir / "artifacts", fetch)
    store = state_dir / "acme" / ("staging" if staging else "production")
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    cmd = [
        str(binary), "--log.format", "text", "run", "--accept-tos", "--no-random-sleep",
        "--server", "letsencrypt-staging" if staging else "letsencrypt",
        "--dns", provider, "--path", str(store), "--cert.name", "bridge",
        "--account-id", "proxy-pipeline", "--domains", f"*.{zone}", "--domains", zone,
    ]
    # The CA validates against the zone's authoritative servers, so only they are awaited.
    # The controller's own recursive resolver is not: it may hold a cached "no such name" from
    # the moment before the record appeared (2026-10-13: that alone failed a rehearsal).
    cmd.append("--dns.propagation.disable-rns")
    resolvers = acme.get("resolvers", ["1.1.1.1:53", "8.8.8.8:53"])
    if not isinstance(resolvers, list) or not all(re.fullmatch(r"[0-9a-fA-F.:\[\]]+:[0-9]+", str(r)) for r in resolvers):
        raise ValueError("acme.resolvers must be a list of host:port")
    for resolver in resolvers:
        cmd += ["--dns.resolvers", str(resolver)]
    email = str(acme.get("email") or "").strip()
    if email:
        cmd += ["--email", email]
    if force:
        cmd.append("--renew-force")
    # The token reaches lego through its environment only: never argv, never a log line.
    env = {**os.environ, PROVIDERS[provider]: auth}
    prefix = PROVIDERS[provider].rsplit("_AUTH_TOKEN", 1)[0]
    # Measured 2026-10-13: three of Timeweb's four name servers serve a new record within
    # seconds, ns3.timeweb.org about twelve minutes later, and the CA may ask any of them.
    env.setdefault(f"{prefix}_PROPAGATION_TIMEOUT", str(int(acme.get("propagation_timeout", 1800))))
    env.setdefault(f"{prefix}_POLLING_INTERVAL", "15")
    # Two names are validated one after the other, each waiting for the zone's servers.
    budget = 2 * int(env[f"{prefix}_PROPAGATION_TIMEOUT"]) + 300
    try:
        proc = runner(cmd, env=env, capture_output=True, text=True, timeout=budget)
    except subprocess.TimeoutExpired as e:
        raise OSError(f"lego did not finish within {budget}s; nothing was published") from e
    if proc.returncode != 0:
        output = (proc.stderr or proc.stdout or "").replace(auth, "<token>")
        (store / "last-failure.log").write_text(output)
        tail = "\n".join(line[:900] for line in output.strip().splitlines()[-4:])
        raise OSError(f"lego failed (exit {proc.returncode}); full log in {store / 'last-failure.log'}:\n{tail}")
    found = {suffix: [p for p in store.rglob(f"bridge{suffix}") if p.is_file()] for suffix in (".crt", ".key")}
    if any(len(paths) != 1 for paths in found.values()):
        raise OSError(f"lego succeeded but bridge.crt/bridge.key were not found exactly once under {store}")
    cert, key = published(state_dir, zone, staging)
    changed = [_publish(found[".crt"][0], cert), _publish(found[".key"][0], key)]
    return {"changed": any(changed), "certificate": str(cert), "private_key": str(key),
            "staging": staging, "names": [f"*.{zone}", zone]}


def describe(cert: Path) -> dict[str, Any]:
    """Subject names and remaining lifetime of a published certificate (openssl)."""
    if not cert.is_file():
        return {"present": False}
    out = subprocess.run(
        ["openssl", "x509", "-in", str(cert), "-noout", "-enddate", "-issuer", "-ext", "subjectAltName"],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        return {"present": True, "readable": False}
    end = re.search(r"notAfter=(.+)", out.stdout)
    days = None
    for limit in (60, 30, 21, 14, 7, 3, 1, 0):
        ok = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-checkend", str(limit * 86400)],
                            capture_output=True, timeout=30).returncode == 0
        if ok:
            days = limit
            break
    return {"present": True, "readable": True, "not_after": end.group(1).strip() if end else "",
            "valid_at_least_days": days, "issuer": (re.search(r"issuer=(.+)", out.stdout) or [None, ""])[1].strip(),
            "names": re.findall(r"DNS:([^,\s]+)", out.stdout)}
