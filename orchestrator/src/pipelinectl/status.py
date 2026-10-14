"""Which node is broken and the command that fixes it: `pipelinectl status`.

The chain has four parts that fail independently: the WORLD Exit (Cherry, Terraform), the RU
bridge (manual server, e.g. VDSina), the bridge's DNS name (Timeweb) and its certificate. This
module maps the checks of `validate --deep` (plus an optional look from outside networks) onto
those parts and names one fix for each broken part. It decides nothing by itself and changes
nothing; the operator runs the printed command.
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from pipelinectl.health import CheckResult

GLOBALPING = "https://api.globalping.io/v1/measurements"
# Below this share of Russian networks reaching the bridge, the address itself is filtered
# (2026-10-13: 10 of 40 for a filtered VDSina address, 40 of 40 for a clean one).
OUTSIDE_MIN_SHARE = 0.6


@dataclass
class Part:
    name: str
    provider: str
    problems: list[str] = field(default_factory=list)
    fix: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _failed(results: dict[str, CheckResult], name: str) -> bool:
    r = results.get(name)
    return r is not None and not r.ok


def _detail(results: dict[str, CheckResult], name: str) -> str:
    r = results.get(name)
    return f": {r.detail}" if r is not None and r.detail else ""


def diagnose(
    checks: list[CheckResult],
    outside: tuple[int, int] | None = None,
    providers: dict[str, str] | None = None,
    declared_bridge: str = "",
    recorded_bridge: str = "",
    bridge_automatic: bool = False,
) -> list[Part]:
    """Parts of the chain with their problems and fix commands, worst evidence first.

    `outside` is (networks that reached the bridge, networks asked) from Russian vantage points.
    `declared_bridge` is bridge_manual.ip from config, `recorded_bridge` the address in state:
    when they differ the checks ran against a stale address and state must be synced first.
    `bridge_automatic`: `rotate bridge` can order a new server itself (VDSina API, Terraform).
    """
    p = {"exit": "Cherry Servers", "bridge": "manual RU server", "dns": "Timeweb DNS", **(providers or {})}
    r = {c.name: c for c in checks}
    exit_ = Part("Exit (WORLD)", p["exit"])
    bridge = Part("Bridge (RU)", p["bridge"])
    dns = Part("DNS name", p["dns"])
    cert = Part("Certificate", "Let's Encrypt via DNS-01")

    if _failed(r, "exit_ip"):
        exit_.problems.append("no Exit in state/runtime.json" + _detail(r, "exit_ip"))
    if _failed(r, "exit_ssh"):
        exit_.problems.append("no SSH from the controller" + _detail(r, "exit_ssh"))
    for name in ("tcp_exit_reality", "tcp_exit_ss", "tcp_exit_chain"):
        if _failed(r, name):
            exit_.problems.append(f"port closed {r[name].detail}")
    if _failed(r, "panel_login"):
        exit_.problems.append("3X-UI panel does not answer" + _detail(r, "panel_login"))
    if _failed(r, "tls_reality"):
        exit_.problems.append("REALITY handshake fails" + _detail(r, "tls_reality"))
    hop, chain, direct = (_failed(r, f"bridge_{n}") for n in ("tls_exit", "chain_http", "direct_http"))
    if chain and not direct:
        exit_.problems.append(
            "the bridge reaches the internet but not through the Exit"
            + (" (handshake to the Exit fails too)" if hop else " (Exit egress or Xray)")
        )
    if _failed(r, "bridge_to_exit_chain"):
        exit_.problems.append("chain port of the Exit is closed from the bridge")

    if _failed(r, "bridge_ssh"):
        bridge.problems.append("no SSH from the controller" + _detail(r, "bridge_ssh"))
    if chain and direct:
        bridge.problems.append("the bridge has no working egress (border filtering or provider)")
    software = False
    stale = bool(declared_bridge and recorded_bridge and declared_bridge != recorded_bridge)
    if stale:
        bridge.problems.append(
            f"state records the bridge at {recorded_bridge}, config (bridge_manual.ip) says {declared_bridge}"
        )
        software = True
    if _failed(r, "bridge_entry_https"):
        bridge.problems.append("HTTPS entry does not answer on the bridge itself (nginx/Xray)")
        software = True
    if _failed(r, "bridge_probe") and not _failed(r, "bridge_ssh"):
        bridge.problems.append("probe round did not run" + _detail(r, "bridge_probe"))
        software = True
    filtered = False
    if outside is not None:
        reached, asked = outside
        if asked and reached / asked < OUTSIDE_MIN_SHARE:
            bridge.problems.append(
                f"only {reached} of {asked} Russian networks reach the bridge address (address filtered)"
            )
            filtered = True

    if _failed(r, "entry_dns"):
        dns.problems.append("name does not point to the bridge" + _detail(r, "entry_dns"))
    if _failed(r, "bridge_entry_cert"):
        cert.problems.append("certificate expires soon or is invalid" + _detail(r, "bridge_entry_cert"))
    if _failed(r, "entry_https") and "certificate" in r["entry_https"].detail:
        cert.problems.append(r["entry_https"].detail)

    if not exit_.ok:
        exit_.fix = ["pipelinectl rotate exit --yes",
                     "(out of stock: pipelinectl stock, then add --region SLUG)"]
    dead = _failed(r, "bridge_ssh") or (chain and direct) or filtered
    if dead and bridge_automatic:
        bridge.fix = ["pipelinectl rotate bridge   (orders, checks and moves to a new server)"]
    elif dead:
        bridge.fix = ["create a new RU server with the controller's SSH key (provider panel)",
                      "pipelinectl rotate bridge --bridge-ip NEW_IP"]
    elif software:
        bridge.fix = ["pipelinectl configure --bridge-only"]
    if stale:
        # Checks above ran against the old address; moving DNS now would point it back there.
        bridge.fix = ["pipelinectl configure --bridge-only", "pipelinectl status"]
        if not dns.ok:
            dns.fix = ["sync state first (see Bridge), then re-run pipelinectl status"]
    elif not dns.ok:
        dns.fix = ["pipelinectl dns ensure --replace"]
    if not cert.ok:
        cert.fix = ["pipelinectl cert renew --deploy"]
    return [exit_, bridge, dns, cert]


def outside_reach(ip: str, host: str, limit: int = 20, timeout: float = 60.0) -> tuple[int, int]:
    """(networks that completed TLS to ip:443, networks asked) from Russia, via Globalping.

    Raises OSError/ValueError when the measurement service cannot be used.
    """
    def call(url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode() if body else None,
            headers={"Content-Type": "application/json", "User-Agent": "proxy-pipeline"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed https URL
            return json.load(resp)

    started = call(GLOBALPING, {
        "type": "http", "target": ip, "limit": limit, "locations": [{"country": "RU"}],
        "measurementOptions": {"protocol": "HTTPS", "port": 443,
                               "request": {"method": "HEAD", "path": "/", "host": host}},
    })
    deadline = time.monotonic() + timeout
    data: dict[str, Any] = {}
    while time.monotonic() < deadline:
        time.sleep(2)
        data = call(f"{GLOBALPING}/{started['id']}")
        if data.get("status") != "in-progress":
            break
    results = data.get("results") or []
    reached = 0
    for item in results:
        res = item.get("result") or {}
        raw = str(res.get("rawOutput") or "").lower()
        reached += bool(res.get("statusCode")) or (res.get("status") == "finished" and "timed out" not in raw)
    return reached, len(results)


def render(parts: list[Part], outside_note: str = "") -> str:
    lines = []
    for part in parts:
        mark = "OK  " if part.ok else "DOWN"
        lines.append(f"[{mark}] {part.name:<14} {part.provider}")
        lines += [f"         - {problem}" for problem in part.problems]
        for i, step in enumerate(part.fix, 1):
            lines.append(f"         fix {i}: {step}")
    if outside_note:
        lines.append(outside_note)
    broken = [p.name for p in parts if not p.ok]
    lines.append("All parts work." if not broken else f"Broken: {', '.join(broken)}.")
    return "\n".join(lines)
