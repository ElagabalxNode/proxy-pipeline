"""Standalone, standard-library-only TLS probe, also executed over SSH on WORLD.

The controller sends candidates on stdin. Keep this module independent of pipelinectl
so the exact tested implementation runs on a node without installing the orchestrator.
"""

from __future__ import annotations

import json
import socket
import ssl
import sys
import time
from typing import Any
from urllib.parse import urlsplit


def target_endpoint(target: dict[str, Any]) -> tuple[str, int]:
    """Dial dest, with SNI kept separately; bracketed IPv6 is supported."""
    dest = str(target.get("dest") or "")
    endpoint = urlsplit("//" + dest)
    if (
        not endpoint.hostname
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.path
        or endpoint.query
        or endpoint.fragment
        or any(c.isspace() for c in dest)
    ):
        raise ValueError("dest must be host:port or [IPv6]:port")
    port = endpoint.port
    if port is None or not 1 <= port <= 65535:
        raise ValueError("dest must include a port between 1 and 65535")
    return endpoint.hostname, port


def probe_target(target: dict[str, Any], timeout: float = 10) -> dict[str, Any]:
    result = {
        "id": target.get("id", ""),
        "sni": target.get("sni", ""),
        "dest": target.get("dest", ""),
        "priority": target.get("priority", 2),
        "tcp_ok": False,
        "tls_ok": False,
        "alpn_ok": False,
        "eligible": False,
        "rtt_ms": None,
        "handshake_ms": None,
        "alpn": "",
        "tls_version": "",
        "peer_ip": "",
        "stage": "config",
        "error": "",
    }
    try:
        host, port = target_endpoint(target)
        sni = target["sni"]
        if not isinstance(sni, str) or not sni or any(c.isspace() for c in sni):
            raise ValueError("sni must be a nonempty hostname")
        expected = target.get("expected_alpn", ["h2"])
        if (
            not isinstance(expected, list)
            or not expected
            or any(not isinstance(p, str) or not p for p in expected)
        ):
            raise ValueError("expected_alpn must be a nonempty list of protocols")
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.set_alpn_protocols(expected)
        result["stage"] = "tcp"
        start = time.monotonic()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            result["tcp_ok"] = True
            result["peer_ip"] = sock.getpeername()[0]
            result["rtt_ms"] = round((time.monotonic() - start) * 1000, 1)
            result["stage"] = "tls"
            # create_default_context retains certificate and hostname verification.
            with ctx.wrap_socket(sock, server_hostname=sni) as tls:
                result["tls_version"] = tls.version() or ""
                result["handshake_ms"] = round((time.monotonic() - start) * 1000, 1)
                if result["tls_version"] != "TLSv1.3":
                    raise ValueError("REALITY target must negotiate TLSv1.3")
                result["tls_ok"] = True
                result["stage"] = "alpn"
                result["alpn"] = tls.selected_alpn_protocol() or ""
                if result["alpn"] not in expected:
                    raise ValueError(f"ALPN {result['alpn']!r} does not match {expected!r}")
                result.update(alpn_ok=True, eligible=True, stage="ok")
    except Exception as exc:  # Each failed candidate must remain visible in the report.
        result["error"] = f"{type(exc).__name__}: {exc}"[:500]
    return result


def main() -> None:
    targets = json.load(sys.stdin)
    print(json.dumps([probe_target(t) for t in targets]))


if __name__ == "__main__":
    main()
