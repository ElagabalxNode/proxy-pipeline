"""DNS records of a zone hosted at Timeweb Cloud: the bridge's A record.

Only what the bridge entry needs: read the zone, create an address record for one host, and
replace it when the bridge moves. The ACME client manages its own TXT records separately.
"""

from __future__ import annotations

import ipaddress
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from pipelinectl import cherry, paths

API = "https://api.timeweb.cloud/api"


class ApiError(OSError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status

Request = Callable[[str, str, dict[str, Any] | None], Any]  # (method, path, body) -> parsed JSON


def token(cfg: dict[str, Any] | None = None) -> str:
    """TIMEWEBCLOUD_AUTH_TOKEN, else TWC_TOKEN; from the environment, then the repository .env."""
    dotenv = cherry.read_kv_file(paths.repo_root() / ".env")
    for name in ("TIMEWEBCLOUD_AUTH_TOKEN", "TWC_TOKEN"):
        value = os.environ.get(name, "").strip() or dotenv.get(name, "").strip()
        if value:
            return value
    return ""


def api(auth: str) -> Request:
    def request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(  # noqa: S310 — fixed https API host
            API + path, data=data, method=method,
            headers={"Authorization": f"Bearer {auth}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:  # noqa: S310
                raw = response.read()
        except urllib.error.HTTPError as e:
            detail = e.read()[:200].decode(errors="replace")
            raise ApiError(e.code, f"Timeweb API {method} {path}: HTTP {e.code} {detail}") from e
        return json.loads(raw) if raw.strip() else {}
    return request


def split_host(host: str, zone: str) -> str:
    """The record name inside the zone; empty for the zone itself (the bare domain)."""
    host, zone = host.rstrip(".").lower(), zone.rstrip(".").lower()
    if host == zone:
        return ""
    if not host.endswith("." + zone):
        raise ValueError(f"{host} is not a host inside {zone}")
    return host[: -len(zone) - 1]


def records(request: Request, name: str) -> list[dict[str, Any]]:
    """Records of exactly this name. Timeweb lists a host's records under the host, not the zone:
    the zone's own list holds only the apex, and a name without records answers 404."""
    try:
        found = request("GET", f"/v1/domains/{name}/dns-records?limit=100", None).get("dns_records", [])
    except ApiError as e:
        if e.status == 404:
            return []
        raise
    def owner(record: dict[str, Any]) -> str:
        # TXT records below the zone (the ACME challenge) are listed with the zone, tagged by a
        # subdomain field; address records of a host are listed under the host itself.
        sub = (record.get("data") or {}).get("subdomain")
        base = record.get("fqdn") or name
        return f"{sub}.{base}" if sub else base

    return [
        {"id": r.get("id"), "type": r.get("type"), "ttl": r.get("ttl"),
         "name": owner(r), "value": (r.get("data") or {}).get("value")}
        for r in found
    ]


def ensure_a(request: Request, zone: str, host: str, address: str, replace: bool = False) -> dict[str, Any]:
    """Make `host` resolve to exactly `address` (IPv4) and nothing else.

    An existing different address is replaced only when asked: pointing the name elsewhere
    moves every client. An AAAA or CNAME record for the host is refused, because it would send
    some clients to a different machine.
    """
    name = split_host(host, zone)
    if not isinstance(ipaddress.ip_address(address), ipaddress.IPv4Address):
        raise ValueError("the bridge is published with an A record only: an IPv4 address is required")
    # The zone's own list also carries TXT records of names below it; keep exactly this name.
    host_records = [r for r in records(request, host) if r["name"] == host.rstrip(".").lower()]
    stray = [r for r in host_records if r["type"] in ("AAAA", "CNAME")]
    if stray:
        raise ValueError(f"{host} has a {stray[0]['type']} record: remove it before publishing the bridge")
    current = [r for r in host_records if r["type"] == "A"]
    if [r["value"] for r in current] == [address]:
        return {"changed": False, "host": host, "address": address}
    if current and not replace:
        raise ValueError(
            f"{host} already points to {', '.join(str(r['value']) for r in current)}; pass --replace to move it"
        )
    for record in current:
        # Like its listing, a host's record is addressed under the host: the zone path answers 404.
        owner = host.rstrip(".").lower()
        request("DELETE", f"/v1/domains/{owner}/dns-records/{record['id']}", None)
    if name:
        request("POST", f"/v1/domains/{zone}/dns-records", {"type": "A", "value": address, "subdomain": name})
    else:
        # The bare domain: v1 insists on a subdomain name, v2 addresses the record by its owner.
        request("POST", f"/v2/domains/{zone}/dns-records", {"type": "A", "value": address})
    return {"changed": True, "host": host, "address": address,
            "replaced": [r["value"] for r in current]}
