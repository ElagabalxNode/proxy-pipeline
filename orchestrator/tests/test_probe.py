from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipelinectl.probe import (
    _score,
    load_reality_target,
    load_target_candidates,
    pick_winner,
    write_reality_target,
)


# ── score ──────────────────────────────────────────────────────────────────

def test_score_failed_tls():
    assert _score({"tls_ok": False, "rtt_ms": 5, "priority": 1}) < 0


def test_score_none_rtt():
    assert _score({"tls_ok": True, "rtt_ms": None, "priority": 1}) < 0


def test_score_priority_1_beats_priority_2_same_rtt():
    high = _score({"tls_ok": True, "rtt_ms": 10, "priority": 1})
    low  = _score({"tls_ok": True, "rtt_ms": 10, "priority": 2})
    assert high > low


# ── pick_winner ─────────────────────────────────────────────────────────────

_RESULTS = [
    {"id": "a", "sni": "a.com", "dest": "a.com:443", "tls_ok": True,  "rtt_ms": 20, "alpn": "h2",       "priority": 2},
    {"id": "b", "sni": "b.com", "dest": "b.com:443", "tls_ok": True,  "rtt_ms": 8,  "alpn": "h2",       "priority": 1},
    {"id": "c", "sni": "c.com", "dest": "c.com:443", "tls_ok": False, "rtt_ms": None, "alpn": "",        "priority": 1},
]


def test_pick_winner_selects_best(tmp_path):
    report = pick_winner(_RESULTS, "1.2.3.4", "NL-Amsterdam")
    assert report["selected"]["id"] == "b"
    assert report["candidates_passed"] == 2
    assert report["candidates_tested"] == 3


def test_pick_winner_raises_if_all_failed():
    bad = [{"id": "x", "sni": "x.com", "dest": "x.com:443", "tls_ok": False, "rtt_ms": None, "alpn": "", "priority": 1}]
    with pytest.raises(RuntimeError, match="failed TLS"):
        pick_winner(bad, "1.2.3.4", "NL")


# ── write / load_reality_target ──────────────────────────────────────────────

def test_write_and_load_roundtrip(tmp_path: Path):
    report = pick_winner(_RESULTS, "5.5.5.5", "DE-Frankfurt")
    write_reality_target(tmp_path, report)
    loaded = load_reality_target(tmp_path)
    assert loaded["selected"]["sni"] == "b.com"
    assert loaded["selected"]["exit_ip"] == "5.5.5.5"


def test_load_reality_target_missing(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_reality_target(tmp_path)


def test_load_reality_target_empty_sni(tmp_path: Path):
    bad = {"selected": {"sni": "", "dest": "x:443"}}
    (tmp_path / "reality_target.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="no 'selected.sni'"):
        load_reality_target(tmp_path)


# ── load_target_candidates ───────────────────────────────────────────────────

def test_load_candidates_from_real_file():
    repo = Path(__file__).resolve().parents[2]
    yaml_path = repo / "config" / "reality_targets.yaml"
    candidates = load_target_candidates(yaml_path)
    # At least some enabled entries
    assert len(candidates) > 3
    # All enabled
    assert all(t.get("enabled", True) for t in candidates)
    # Sorted by priority ascending
    priorities = [int(t.get("priority", 2)) for t in candidates]
    assert priorities == sorted(priorities)
