"""probe-targets: measure RTT + TLS from Exit VPS and pick best Reality target."""

from __future__ import annotations

import json
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

def _passed(r: dict[str, Any]) -> bool:
    # The optional fields retain compatibility with historical reports/tests.
    return bool(
        r.get("tls_ok") and r.get("alpn_ok", True) and r.get("eligible", True)
        and r.get("rtt_ms") is not None
    )


def _score(r: dict[str, Any]) -> float:
    """Higher is better. Returns <0 if target failed TLS."""
    if not _passed(r):
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
    fields = ["id", "sni", "dest", "priority", "expected_alpn"]
    slim = [{k: t[k] for k in fields if k in t} for t in targets]
    script = Path(__file__).with_name("target_probe.py").read_text(encoding="utf-8")

    ssh_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-i", str(exit_key),
        f"{exit_user}@{exit_ip}",
        f"python3 -c {shlex.quote(script)}",
    ]
    timeout = 30 + len(targets) * 25  # TCP + TLS timeout per candidate, plus SSH overhead.
    try:
        proc = subprocess.run(
            ssh_cmd,
            input=json.dumps(slim),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"SSH probe timed out ({timeout} s)") from e
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
    passed = [r for r in results if _passed(r)]
    if not passed:
        failing = [r["id"] for r in results]
        raise RuntimeError(
            f"All {len(failing)} probed targets failed TLS/ALPN eligibility: {failing}. "
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
            "tls_version": winner.get("tls_version", ""),
            "handshake_ms": winner.get("handshake_ms"),
            "peer_ip": winner.get("peer_ip", ""),
            "probe_from": "world",
            "timestamp": now,
            "exit_ip": exit_ip,
            "exit_region": exit_region,
        },
        "candidates_tested": len(results),
        "candidates_passed": len(passed),
        "all_results": results,
    }


def _target_state_path(state_dir: Path, purpose: str) -> Path:
    if purpose not in {"direct", "chain"}:
        raise ValueError("target purpose must be 'direct' or 'chain'")
    return state_dir / ("chain_reality_target.json" if purpose == "chain" else "reality_target.json")


def write_reality_target(
    state_dir: Path, report: dict[str, Any], purpose: str = "direct",
) -> Path:
    out = _target_state_path(state_dir, purpose)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def load_reality_target(state_dir: Path, purpose: str = "direct") -> dict[str, Any]:
    """Read state/reality_target.json; raise FileNotFoundError if absent."""
    p = _target_state_path(state_dir, purpose)
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
