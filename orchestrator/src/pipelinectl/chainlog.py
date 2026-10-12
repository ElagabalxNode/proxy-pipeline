"""Read the bridge chain-probe log (mirrored into state/probe/) and say WHERE stalls happened.

The probe (ansible/roles/bridge_xray_relay/templates/chain-probe.sh.j2) writes one JSON line per
check per round, three checks per round sharing one `ts`:

  tls_exit     TLS handshake bridge -> Exit chain inbound   (the hop that crosses the RU border)
  chain_http   HTTP through the whole chain (bridge -> Exit -> internet)
  direct_http  HTTP from the bridge directly, no Exit       (reference)
  dest_tls     TLS to the bridge's own Reality dest         (client handshakes stall if this fails)
"""

from __future__ import annotations

import gzip
import json
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CHECKS = ("tls_exit", "chain_http", "direct_http", "dest_tls")
REMOTE_LOG_DIR = "/var/log/chain-probe/"
LOG_BASENAME = "probe.jsonl"

_SINCE_RE = re.compile(r"^\s*(\d+)\s*([mhd])\s*$")


def parse_since(spec: str) -> timedelta:
    """'90m' | '24h' | '7d' -> timedelta."""
    m = _SINCE_RE.match(spec or "")
    if not m:
        raise ValueError(f"bad --since '{spec}': use e.g. 90m, 24h, 7d")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: n})


def _parse_ts(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def load_records(log_dir: Path, since: datetime | None = None) -> list[dict[str, Any]]:
    """All records from probe.jsonl, probe.jsonl.1, probe.jsonl.*.gz newer than `since`, sorted."""
    out: list[dict[str, Any]] = []
    for path in sorted(log_dir.glob(LOG_BASENAME + "*")):
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as fh:  # type: ignore[operator]
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = _parse_ts(str(rec.get("ts", "")))
                    if ts is None or rec.get("check") not in CHECKS:
                        continue
                    if since and ts < since:
                        continue
                    rec["_ts"] = ts
                    out.append(rec)
        except OSError:
            continue
    out.sort(key=lambda r: (r["_ts"], CHECKS.index(r["check"])))
    return out


def _pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def summarize(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per check: n, fail, fail_pct, p50/p95/max of ms over successful rounds."""
    out: dict[str, dict[str, Any]] = {}
    for c in CHECKS:
        rs = [r for r in records if r["check"] == c]
        oks = [int(r.get("ms") or 0) for r in rs if r.get("ok") is True]
        fail = sum(1 for r in rs if r.get("ok") is not True)
        out[c] = {
            "n": len(rs),
            "fail": fail,
            "fail_pct": (100.0 * fail / len(rs)) if rs else 0.0,
            "p50": _pct(oks, 0.50),
            "p95": _pct(oks, 0.95),
            "max": max(oks) if oks else 0,
        }
    return out


@dataclass
class Window:
    start: datetime
    end: datetime
    rounds: int = 0
    failed: Counter = field(default_factory=Counter)  # check -> failing rounds
    patterns: Counter = field(default_factory=Counter)  # frozenset(failed checks) -> rounds

    @property
    def verdict(self) -> str:
        pattern = set(self.patterns.most_common(1)[0][0]) if self.patterns else set()
        return verdict_with_dest(pattern)


def verdict(failed: set[str]) -> str:
    chain, hop, direct = "chain_http" in failed, "tls_exit" in failed, "direct_http" in failed
    dest = "dest_tls" in failed
    if dest and not (chain or hop or direct):
        return "Reality dest unreachable from the bridge — client handshakes stall (change bridge SNI/dest)"
    if chain and direct:
        return "bridge egress / RU border — everything from the bridge failed (clients down)"
    if chain and hop:
        return "bridge→Exit hop — TLS to the Exit failed too (border DPI or Exit down)"
    if chain:
        return "Exit side — hop TLS fine, but nothing gets out through the Exit (Exit egress/xray)"
    if hop:
        return "hop TLS handshake only — chain HTTP still passed (transient / slow handshake)"
    if direct:
        return "bridge direct egress only — clients unaffected"
    return "ok"


def verdict_with_dest(failed: set[str]) -> str:
    base = verdict(failed - {"dest_tls"}) if failed - {"dest_tls"} else "ok"
    if "dest_tls" in failed and base != "ok":
        return base + " + Reality dest down (client handshakes stall)"
    return verdict(failed)


def failure_windows(records: list[dict[str, Any]], gap_s: int = 150) -> list[Window]:
    """Group rounds (same ts) with any failed check into windows split by gaps > gap_s."""
    rounds: dict[datetime, set[str]] = {}
    for r in records:
        rounds.setdefault(r["_ts"], set())
        if r.get("ok") is not True:
            rounds[r["_ts"]].add(str(r["check"]))
    windows: list[Window] = []
    cur: Window | None = None
    for ts in sorted(rounds):
        failed = rounds[ts]
        if not failed:
            continue
        if cur and (ts - cur.end).total_seconds() <= gap_s:
            cur.end = ts
        else:
            cur = Window(start=ts, end=ts)
            windows.append(cur)
        cur.rounds += 1
        cur.failed.update(failed)
        cur.patterns[frozenset(failed)] += 1
    return windows


def pull(local_dir: Path, bridge_ip: str, ssh_user: str, key: Path) -> None:
    """Mirror the bridge's log directory now (what the controller timer does every few minutes)."""
    local_dir.mkdir(parents=True, exist_ok=True)
    ssh = f"ssh -i {key} -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=15"
    subprocess.run(
        [
            "rsync",
            "-az",
            "--timeout=60",
            "-e",
            ssh,
            f"{ssh_user}@{bridge_ip}:{REMOTE_LOG_DIR}",
            f"{local_dir}/",
        ],
        check=True,
    )


def render_text(records: list[dict[str, Any]], since_label: str, max_windows: int = 10) -> str:
    if not records:
        return f"no probe records in the last {since_label} — is the pull timer running? (pipelinectl chainlog --pull)"
    first, last = records[0]["_ts"], records[-1]["_ts"]
    n_rounds = len({r["_ts"] for r in records})
    lines = [
        f"chain-probe — last {since_label}: {n_rounds} rounds, "
        f"{first:%Y-%m-%d %H:%M}Z → {last:%Y-%m-%d %H:%M}Z",
        f"  {'CHECK':<12}{'N':>6}{'FAIL':>6}{'FAIL%':>7}{'p50ms':>8}{'p95ms':>8}{'max':>8}",
    ]
    for c, s in summarize(records).items():
        lines.append(
            f"  {c:<12}{s['n']:>6}{s['fail']:>6}{s['fail_pct']:>6.1f}%{s['p50']:>8}{s['p95']:>8}{s['max']:>8}"
        )
    wins = failure_windows(records)
    lines.append("")
    if not wins:
        lines.append("failure windows: none — every round passed")
        return "\n".join(lines)
    total_rounds = sum(w.rounds for w in wins)
    lines.append(f"failure windows: {len(wins)} ({total_rounds} failing rounds), newest first:")
    for w in sorted(wins, key=lambda w: w.start, reverse=True)[:max_windows]:
        span = (
            f"{w.start:%m-%d %H:%M}Z → {w.end:%H:%M}Z"
            if w.start != w.end
            else f"{w.start:%m-%d %H:%M}Z"
        )
        fails = " ".join(f"{c}×{n}" for c, n in w.failed.most_common())
        lines.append(f"  {span:<22} {w.rounds:>3} rounds  {fails:<40} → {w.verdict}")
    return "\n".join(lines)
