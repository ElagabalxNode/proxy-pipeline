"""probe-targets: measure RTT + TLS from Exit VPS and pick best Reality target."""

from __future__ import annotations

import base64
import json
import shlex
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# ─── Remote probe script (runs as `python3 -c <code>` on Exit VPS) ──────────
# Embedded targets JSON is passed via base64 to avoid shell quoting issues.
_REMOTE_PROBE_TEMPLATE = textwrap.dedent("""\
    import json, base64, ssl, socket, time
    targets = json.loads(base64.b64decode({b64}).decode())
    results = []
    for t in targets:
        host = t['sni']
        port = int(t.get('port', 443))
        tls_ok = False
        rtt_ms = None
        alpn = ''
        try:
            t0 = time.monotonic()
            sock = socket.create_connection((host, port), timeout=10)
            t1 = time.monotonic()
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                alpn = tls.selected_alpn_protocol() or ''
                tls_ok = True
            sock.close()
            rtt_ms = round((t1 - t0) * 1000, 1)
        except Exception as exc:
            pass
        results.append({{
            'id': t['id'], 'sni': t['sni'], 'dest': t['dest'],
            'tls_ok': tls_ok, 'rtt_ms': rtt_ms, 'alpn': alpn,
            'priority': int(t.get('priority', 2)),
        }})
    print(json.dumps(results))
""")


def _score(r: dict[str, Any]) -> float:
    """Higher is better. Returns <0 if target failed TLS."""
    if not r.get("tls_ok") or r.get("rtt_ms") is None:
        return -1.0
    rtt_score = max(0.0, 100.0 - float(r["rtt_ms"]))
    priority_bonus = (2 - int(r.get("priority", 2))) * 10.0
    return rtt_score + priority_bonus


def load_target_candidates(targets_yaml: Path) -> list[dict[str, Any]]:
    """Return enabled targets from reality_targets.yaml, sorted priority asc."""
    raw = yaml.safe_load(targets_yaml.read_text(encoding="utf-8")) or {}
    targets = raw.get("targets") or []
    enabled = [t for t in targets if t.get("enabled", True)]
    return sorted(enabled, key=lambda t: int(t.get("priority", 2)))


def probe_targets_on_exit(
    targets: list[dict[str, Any]],
    exit_ip: str,
    exit_user: str,
    exit_key: Path,
) -> list[dict[str, Any]]:
    """SSH to Exit VPS and run TLS+RTT probes for each target. Returns raw results."""
    fields = ["id", "sni", "dest", "port", "priority"]
    slim = [{k: t[k] for k in fields if k in t} for t in targets]
    b64 = base64.b64encode(json.dumps(slim).encode()).decode()
    script = _REMOTE_PROBE_TEMPLATE.format(b64=repr(b64))

    ssh_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-i", str(exit_key),
        f"{exit_user}@{exit_ip}",
        f"python3 -c {shlex.quote(script)}",
    ]
    try:
        proc = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("SSH probe timed out (180 s)") from e
    if proc.returncode != 0:
        raise RuntimeError(
            f"SSH probe failed (exit {proc.returncode}):\n"
            + (proc.stderr or proc.stdout or "").strip()[:1000]
        )
    raw = (proc.stdout or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Probe returned non-JSON output:\n{raw[:500]}"
        ) from e


def pick_winner(
    results: list[dict[str, Any]],
    exit_ip: str,
    exit_region: str,
) -> dict[str, Any]:
    """Select best target; raise RuntimeError if no TLS-ok candidates."""
    passed = [r for r in results if r.get("tls_ok")]
    if not passed:
        failing = [r["id"] for r in results]
        raise RuntimeError(
            f"All {len(failing)} probed targets failed TLS: {failing}. "
            "Check Exit network connectivity and firewall."
        )
    winner = max(passed, key=_score)
    score = _score(winner)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "selected": {
            "id": winner["id"],
            "sni": winner["sni"],
            "dest": winner["dest"],
            "score": round(score, 1),
            "measured_rtt_ms": winner.get("rtt_ms"),
            "tls_ok": True,
            "alpn": winner.get("alpn", ""),
            "timestamp": now,
            "exit_ip": exit_ip,
            "exit_region": exit_region,
        },
        "candidates_tested": len(results),
        "candidates_passed": len(passed),
        "all_results": results,
    }


def write_reality_target(state_dir: Path, report: dict[str, Any]) -> Path:
    out = state_dir / "reality_target.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def load_reality_target(state_dir: Path) -> dict[str, Any]:
    """Read state/reality_target.json; raise FileNotFoundError if absent."""
    p = state_dir / "reality_target.json"
    if not p.exists():
        raise FileNotFoundError(
            f"{p} not found — run 'pipelinectl probe-targets' before 'configure'."
        )
    data = json.loads(p.read_text(encoding="utf-8"))
    selected = data.get("selected") or {}
    if not selected.get("sni"):
        raise ValueError(
            f"{p} exists but has no 'selected.sni'. "
            "Re-run 'pipelinectl probe-targets'."
        )
    return data
