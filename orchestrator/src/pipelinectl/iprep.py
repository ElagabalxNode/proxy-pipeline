"""Reputation of a fresh Exit IP: what Shodan's free InternetDB still remembers about it.

Cherry hands out recycled addresses (leased via IPXO). 2026-09-12: a fresh NL Exit inherited an IP
whose previous tenant ran an NJRat C2 (Netcraft issue #94110554, "reappeared"); the provider
null-routed the server within a day. InternetDB (no API key) lags reality by days — which is
exactly what makes it a pre-check: it shows the PREVIOUS tenant's open ports, tags and hostnames,
not ours. Mail DNSBLs do not catch this (they track spam, not C2).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

INTERNETDB = "https://internetdb.shodan.io/"
# What our own Exit exposes: SSH, VLESS+Reality, 3X-UI panel, SS-2022, chain inbound.
EXIT_PORTS = frozenset({22, 443, 2053, 8388, 8443})
BAD_TAGS = frozenset(
    {"malware", "c2", "botnet", "honeypot", "compromised", "tor", "proxy", "vpn", "scanner", "spam"}
)
OWN_PTR_SUFFIX = ".ptr.cherryservers.net"


def fetch(ip: str, timeout: int = 20) -> tuple[dict[str, Any] | None, str]:
    """(record, error). record=None with error='' means InternetDB has no data for the IP."""
    # InternetDB answers curl but returns 403 to the default "Python-urllib" User-Agent.
    req = urllib.request.Request(
        INTERNETDB + ip,
        headers={
            "User-Agent": "proxy-pipeline-ipcheck/1.0 (+https://github.com/ElagabalxNode/proxy-pipeline)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r), ""
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, ""
        return None, f"HTTP {e.code}"
    except (OSError, json.JSONDecodeError) as e:
        return None, str(e)


MIN_FOREIGN_PORTS = 2  # one stray port (rpcbind, a printer) is sloppiness, not a C2 history


def assess(
    data: dict[str, Any] | None,
    allowed_ports: frozenset[int] = EXIT_PORTS,
    own_ptr_suffix: str = OWN_PTR_SUFFIX,
    min_foreign_ports: int = MIN_FOREIGN_PORTS,
) -> tuple[bool, str]:
    """(clean, reason). Dirt = >= min_foreign_ports foreign ports, or a bad tag, or a foreign hostname."""
    if not data:
        return True, "no InternetDB record (never scanned or scrubbed)"
    ports: list[int] = []
    for p in data.get("ports") or []:
        try:
            ports.append(int(p))
        except (TypeError, ValueError):
            continue
    foreign_ports = sorted(p for p in ports if p not in allowed_ports)
    tags = sorted({str(t).lower() for t in data.get("tags") or []} & BAD_TAGS)
    hosts = sorted(h for h in (data.get("hostnames") or []) if not str(h).endswith(own_ptr_suffix))
    reasons = []
    if len(foreign_ports) >= min_foreign_ports:
        reasons.append(f"previous tenant's services on ports {foreign_ports}")
    if tags:
        reasons.append(f"tags {tags}")
    if hosts:
        reasons.append(f"foreign hostnames {hosts}")
    if reasons:
        return False, "; ".join(reasons)
    if foreign_ports:
        return True, f"one stray port {foreign_ports} tolerated; otherwise only our own ports"
    return True, f"only our own ports {sorted(ports) or '[]'}"
