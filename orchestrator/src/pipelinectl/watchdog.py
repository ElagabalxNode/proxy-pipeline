"""Self-healing watchdog: when the chain is down because of the Exit, rotate the Exit — the
standard rotation with all its gates (stock, plan guard, bridge reachability, IP reputation,
re-rolls, end-to-end validate) — and only then bother a human.

Rails, because an automatic `terraform apply -replace` must never run in a loop:
  * only for Exit-shaped verdicts (hop TLS failed / Exit side); a dead bridge egress or an
    unreachable bridge cannot be fixed by a new Exit -> alert immediately
  * the outage is confirmed by a LIVE probe round on the bridge right before rotating (the
    mirrored log may be 5 minutes stale)
  * cooldown between auto-rotations and a hard cap per 24 h
  * never while another rotation (manual or automatic) holds state/.rotate.lock
  * same region first, then optional fallback regions, then give up and alert
"""

from __future__ import annotations

import fcntl
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pipelinectl import alerts, chainlog

LOCK_FILE = ".rotate.lock"
EXIT_MARKERS = ("bridge→Exit hop", "Exit side")
BRIDGE_MARKERS = ("bridge egress",)


@dataclass
class Rails:
    auto_rotate: bool = True
    cooldown_hours: float = 2.0
    max_per_day: int = 3
    fallback_regions: tuple[str, ...] = ()
    notify_on_success: bool = True


def rails_from_config(cfg: dict[str, Any]) -> Rails:
    w = cfg.get("watchdog") or {}
    return Rails(
        auto_rotate=bool(w.get("auto_rotate", True)),
        cooldown_hours=float(w.get("cooldown_hours", 2)),
        max_per_day=int(w.get("max_per_day", 3)),
        fallback_regions=tuple(str(r) for r in (w.get("fallback_regions") or [])),
        notify_on_success=bool(w.get("notify_on_success", True)),
    )


def rotation_would_help(detail: str) -> bool:
    return any(m in detail for m in EXIT_MARKERS)


def rails_allow(state: dict[str, Any], now: datetime, rails: Rails) -> tuple[bool, str]:
    hist = [datetime.fromisoformat(t) for t in state.get("auto_rotations") or []]
    last_day = [t for t in hist if now - t < timedelta(days=1)]
    if len(last_day) >= rails.max_per_day:
        return False, f"{len(last_day)} auto-rotations in the last 24h (max {rails.max_per_day})"
    if hist and now - max(hist) < timedelta(hours=rails.cooldown_hours):
        return False, f"cooldown: last auto-rotation at {max(hist):%Y-%m-%d %H:%M}Z"
    return True, ""


def rotation_in_progress(state_dir: Path) -> bool:
    """True if a rotate-chain.sh holds the lock (manual or ours)."""
    p = state_dir / LOCK_FILE
    try:
        with open(p, "a+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return False
    try:
        return (
            subprocess.run(
                ["pgrep", "-f", "scripts/rotate-chain.sh"], capture_output=True
            ).returncode
            == 0
        )
    except OSError:
        return False


def classify_live_round(recs: list[dict[str, Any]]) -> tuple[str, str]:
    """('ok'|'exit'|'bridge', verdict) from one live probe round on the bridge."""
    failed = {str(r.get("check")) for r in recs if r.get("ok") is not True}
    if not ({"chain_http", "tls_exit"} & failed):
        return "ok", "live probe round on the bridge passed"
    v = chainlog.verdict(failed)
    return ("bridge" if any(m in v for m in BRIDGE_MARKERS) else "exit"), v


def rotation_command(repo_root: Path, region: str | None) -> list[str]:
    cmd = ["bash", str(repo_root / "scripts" / "rotate-chain.sh"), "exit", "--yes"]
    if region:
        cmd += ["--region", region]
    return cmd


def run_rotation(
    repo_root: Path, region: str | None, log_path: Path, runner: Any = subprocess.run
) -> tuple[bool, str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        log.write(
            f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} rotate exit region={region or 'same'} =====\n".encode()
        )
        log.flush()
        try:
            p = runner(
                rotation_command(repo_root, region),
                cwd=str(repo_root),
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                timeout=3600,
            )
        except subprocess.TimeoutExpired:
            return False, "timeout after 3600s"
    return p.returncode == 0, f"rc={p.returncode}"


@dataclass
class Outcome:
    kind: (
        str | None
    )  # None | down | stale | recovered | down-reminder | auto-rotated | auto-rotation-failed
    detail: str
    state: dict[str, Any] = field(default_factory=dict)
    attempts: list[tuple[str, bool, str]] = field(default_factory=list)  # (region label, ok, info)


def run(
    verdict: alerts.Verdict,
    prev: dict[str, Any],
    now: datetime,
    rails: Rails,
    live_check: Callable[[], tuple[bool, str, list[dict[str, Any]]]],
    rotate: Callable[[str | None], tuple[bool, str]],
    in_progress: Callable[[], bool],
    rotate_enabled: bool = True,
) -> Outcome:
    """Pure decision core: everything with side effects is injected."""
    kind, state = alerts.decide(prev, verdict, now)
    if verdict.status != "down" or not (rails.auto_rotate and rotate_enabled):
        return Outcome(kind, verdict.detail, state)
    if in_progress():
        return Outcome(None, "a rotation is already running — waiting for it", prev)
    if not rotation_would_help(verdict.detail):
        return Outcome(kind, verdict.detail + " (a new Exit would not help)", state)
    allowed, why = rails_allow(prev, now, rails)
    if not allowed:
        return Outcome(kind, f"{verdict.detail}; auto-rotation blocked: {why}", state)
    ok, msg, recs = live_check()
    if not ok:
        return Outcome(
            kind, f"{verdict.detail}; bridge unreachable for the live check ({msg})", state
        )
    cls, live_detail = classify_live_round(recs)
    if cls == "ok":
        fresh = alerts.Verdict("ok", "live probe round passed (mirror was stale)")
        k2, s2 = alerts.decide(prev, fresh, now)
        return Outcome(k2, fresh.detail, s2)
    if cls == "bridge":
        return Outcome(kind, f"{live_detail} (live) — a new Exit would not help", state)

    attempts: list[tuple[str, bool, str]] = []
    hist = list(prev.get("auto_rotations") or [])[-9:] + [now.isoformat()]
    for region in (None, *rails.fallback_regions):
        ok_r, info = rotate(region)
        attempts.append((region or "same region", ok_r, info))
        if ok_r:
            new_state = dict(
                state,
                status="ok",
                detail="recovered by auto-rotation",
                auto_rotations=hist,
                last_sent=now.isoformat(),
            )
            return Outcome(
                "auto-rotated" if rails.notify_on_success else None,
                live_detail,
                new_state,
                attempts,
            )
    failed_state = dict(state, auto_rotations=hist, last_sent=now.isoformat())
    return Outcome("auto-rotation-failed", live_detail, failed_state, attempts)


def compose(
    outcome: Outcome, verdict: alerts.Verdict, exit_ip: str, bridge_ip: str, log_path: Path | None
) -> tuple[str, str]:
    tries = "\n".join(
        f"  - {r}: {'OK' if ok else 'FAILED'} ({info})" for r, ok, info in outcome.attempts
    )
    if outcome.kind == "auto-rotated":
        return (
            "[proxy-pipeline] auto-rotated the Exit — chain recovered",
            f"Reason: {outcome.detail}\nAttempts:\n{tries}\nNew Exit: {exit_ip}   Bridge: {bridge_ip}\nClient QR unchanged.\nLog: {log_path}\n",
        )
    if outcome.kind == "auto-rotation-failed":
        return (
            "[proxy-pipeline] DOWN — automatic Exit rotation FAILED, manual action needed",
            f"Reason: {outcome.detail}\nAttempts:\n{tries}\nExit: {exit_ip}   Bridge: {bridge_ip}\n"
            f"Log: {log_path}\n\nOn the controller:\n  pipelinectl chainlog --since 2h\n  pipelinectl stock\n  pipelinectl rotate exit --region <other>\n",
        )
    subj, body = alerts.compose(outcome.kind or "down", verdict, exit_ip, bridge_ip)
    if outcome.detail and outcome.detail != verdict.detail:
        body = f"{outcome.detail}\n\n" + body
    return subj, body
