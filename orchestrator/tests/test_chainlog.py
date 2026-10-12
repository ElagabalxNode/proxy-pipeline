from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipelinectl import chainlog

T0 = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)


def _round(ts: datetime, tls=True, chain=True, direct=True, ms=(640, 900, 120)) -> list[str]:
    s = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    return [
        json.dumps({"ts": s, "check": "tls_exit", "ok": tls, "ms": ms[0], "code": "", "err": ""}),
        json.dumps(
            {"ts": s, "check": "chain_http", "ok": chain, "ms": ms[1], "code": "204", "err": ""}
        ),
        json.dumps(
            {"ts": s, "check": "direct_http", "ok": direct, "ms": ms[2], "code": "204", "err": ""}
        ),
    ]


def _write_log(d: Path) -> None:
    lines: list[str] = []
    for i in range(30):  # 30 healthy minutes
        lines += _round(T0 + timedelta(minutes=i))
    # window A (min 30-33): hop + chain fail, direct ok  -> hop verdict
    for i in range(30, 34):
        lines += _round(T0 + timedelta(minutes=i), tls=False, chain=False)
    lines += _round(T0 + timedelta(minutes=34))
    lines += _round(T0 + timedelta(minutes=35))
    # window B (min 36): everything fails -> bridge egress
    lines += _round(T0 + timedelta(minutes=36), tls=False, chain=False, direct=False)
    # window C (min 50): only chain fails -> Exit side
    for i in range(37, 50):
        lines += _round(T0 + timedelta(minutes=i))
    lines += _round(T0 + timedelta(minutes=50), chain=False)
    (d / "probe.jsonl").write_text("\n".join(lines) + "\nnot json\n", encoding="utf-8")
    # an older rotated + gzipped file, one healthy round a day earlier
    with gzip.open(d / "probe.jsonl.2.gz", "wt", encoding="utf-8") as fh:
        fh.write("\n".join(_round(T0 - timedelta(days=1))) + "\n")


def test_parse_since() -> None:
    assert chainlog.parse_since("90m") == timedelta(minutes=90)
    assert chainlog.parse_since("24h") == timedelta(hours=24)
    assert chainlog.parse_since("7d") == timedelta(days=7)
    with pytest.raises(ValueError):
        chainlog.parse_since("soon")


def test_load_reads_plain_and_gz_and_filters(tmp_path: Path) -> None:
    _write_log(tmp_path)
    allrec = chainlog.load_records(tmp_path)
    assert len(allrec) == 3 * (51 + 1)  # 51 rounds today + 1 in the gz
    recent = chainlog.load_records(tmp_path, since=T0)
    assert len(recent) == 3 * 51 and recent[0]["_ts"] == T0


def test_summary_and_windows(tmp_path: Path) -> None:
    _write_log(tmp_path)
    rec = chainlog.load_records(tmp_path, since=T0)
    s = chainlog.summarize(rec)
    assert (
        s["tls_exit"]["fail"] == 5
        and s["chain_http"]["fail"] == 6
        and s["direct_http"]["fail"] == 1
    )
    assert s["chain_http"]["p50"] == 900 and s["direct_http"]["p95"] == 120
    wins = chainlog.failure_windows(rec)
    assert [w.rounds for w in wins] == [4, 1, 1]
    assert "hop" in wins[0].verdict
    assert "bridge egress" in wins[1].verdict
    assert "Exit side" in wins[2].verdict
    assert wins[0].start == T0 + timedelta(minutes=30) and wins[0].end == T0 + timedelta(minutes=33)


def test_render_text(tmp_path: Path) -> None:
    _write_log(tmp_path)
    txt = chainlog.render_text(chainlog.load_records(tmp_path, since=T0), "24h")
    assert "51 rounds" in txt and "failure windows: 3" in txt
    assert "chain_http" in txt and "→ bridge→Exit hop" in txt
    assert chainlog.render_text([], "1h").startswith("no probe records")


def test_verdict_matrix() -> None:
    assert chainlog.verdict(set()) == "ok"
    assert "clients unaffected" in chainlog.verdict({"direct_http"})
    assert "transient" in chainlog.verdict({"tls_exit"})


def test_dest_tls_only_failure_has_its_own_verdict(tmp_path: Path) -> None:
    lines: list[str] = []
    for i in range(3):
        ts = (T0 + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines += _round(T0 + timedelta(minutes=i))
        lines.append(
            json.dumps(
                {"ts": ts, "check": "dest_tls", "ok": i != 1, "ms": 90, "code": "", "err": ""}
            )
        )
    (tmp_path / "probe.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    rec = chainlog.load_records(tmp_path, since=T0)
    assert chainlog.summarize(rec)["dest_tls"] == {
        "n": 3,
        "fail": 1,
        "fail_pct": pytest.approx(100 / 3),
        "p50": 90,
        "p95": 90,
        "max": 90,
    }
    wins = chainlog.failure_windows(rec)
    assert len(wins) == 1 and "Reality dest" in wins[0].verdict
    assert "Reality dest down" in chainlog.verdict_with_dest({"dest_tls", "chain_http", "tls_exit"})
