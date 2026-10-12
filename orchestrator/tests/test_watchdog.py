from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipelinectl import alerts, watchdog

T0 = datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc)
DOWN_EXIT = alerts.Verdict(
    "down",
    "bridge→Exit hop — TLS to the Exit failed too (border DPI or Exit down)",
    T0 - timedelta(minutes=5),
)
DOWN_BRIDGE = alerts.Verdict(
    "down", "bridge egress / RU border — everything from the bridge failed (clients down)", T0
)


def _round(failed: set[str]) -> list[dict]:
    return [
        {"check": c, "ok": c not in failed}
        for c in ("tls_exit", "chain_http", "direct_http", "dest_tls")
    ]


def _run(verdict, prev=None, rails=None, live=None, rotate=None, in_progress=False, **kw):
    calls: list[str | None] = []

    def _rotate(region):
        calls.append(region)
        return (rotate or (lambda r: (True, "rc=0")))(region)

    out = watchdog.run(
        verdict,
        prev or {},
        T0,
        rails or watchdog.Rails(),
        live_check=live or (lambda: (True, "", _round({"tls_exit", "chain_http"}))),
        rotate=_rotate,
        in_progress=lambda: in_progress,
        **kw,
    )
    return out, calls


def test_exit_down_rotates_same_region_and_reports_success() -> None:
    out, calls = _run(DOWN_EXIT)
    assert calls == [None] and out.kind == "auto-rotated"
    assert out.state["status"] == "ok" and len(out.state["auto_rotations"]) == 1


def test_fallback_regions_after_same_region_fails() -> None:
    out, calls = _run(
        DOWN_EXIT,
        rails=watchdog.Rails(fallback_regions=("LT-Siauliai", "DE-Frankfurt")),
        rotate=lambda r: (r == "LT-Siauliai", "rc=1" if r != "LT-Siauliai" else "rc=0"),
    )
    assert calls == [None, "LT-Siauliai"] and out.kind == "auto-rotated"
    assert [a[1] for a in out.attempts] == [False, True]


def test_all_attempts_fail_then_alert() -> None:
    out, calls = _run(
        DOWN_EXIT,
        rails=watchdog.Rails(fallback_regions=("LT-Siauliai",)),
        rotate=lambda r: (False, "rc=1"),
    )
    assert calls == [None, "LT-Siauliai"] and out.kind == "auto-rotation-failed"
    assert out.state["status"] == "down" and out.state["auto_rotations"]


def test_bridge_shaped_verdict_alerts_without_rotating() -> None:
    out, calls = _run(DOWN_BRIDGE)
    assert calls == [] and out.kind == "down" and "would not help" in out.detail


def test_live_round_ok_means_false_alarm() -> None:
    out, calls = _run(DOWN_EXIT, live=lambda: (True, "", _round(set())))
    assert calls == [] and out.kind is None and out.state["status"] == "ok"


def test_live_round_says_bridge_egress() -> None:
    out, calls = _run(
        DOWN_EXIT, live=lambda: (True, "", _round({"tls_exit", "chain_http", "direct_http"}))
    )
    assert calls == [] and out.kind == "down" and "would not help" in out.detail


def test_bridge_unreachable_alerts() -> None:
    out, calls = _run(DOWN_EXIT, live=lambda: (False, "ssh timeout", []))
    assert calls == [] and out.kind == "down" and "bridge unreachable" in out.detail


def test_cooldown_and_daily_cap() -> None:
    recent = {"status": "ok", "auto_rotations": [(T0 - timedelta(minutes=30)).isoformat()]}
    out, calls = _run(DOWN_EXIT, prev=recent)
    assert calls == [] and "cooldown" in out.detail and out.kind == "down"
    capped = {
        "status": "ok",
        "auto_rotations": [(T0 - timedelta(hours=h)).isoformat() for h in (3, 6, 9)],
    }
    out, calls = _run(DOWN_EXIT, prev=capped)
    assert calls == [] and "max 3" in out.detail
    old = {"status": "ok", "auto_rotations": [(T0 - timedelta(hours=30)).isoformat()]}
    out, calls = _run(DOWN_EXIT, prev=old)
    assert calls == [None] and out.kind == "auto-rotated"


def test_never_while_another_rotation_runs() -> None:
    out, calls = _run(DOWN_EXIT, in_progress=True)
    assert calls == [] and out.kind is None and "already running" in out.detail


def test_rotate_disabled_falls_back_to_plain_alert() -> None:
    out, calls = _run(DOWN_EXIT, rotate_enabled=False)
    assert calls == [] and out.kind == "down"
    out, calls = _run(DOWN_EXIT, rails=watchdog.Rails(auto_rotate=False))
    assert calls == [] and out.kind == "down"


def test_rotation_command_and_runner(tmp_path: Path) -> None:
    assert watchdog.rotation_command(Path("/r"), None)[-2:] == ["exit", "--yes"]
    assert watchdog.rotation_command(Path("/r"), "LT-Siauliai")[-2:] == ["--region", "LT-Siauliai"]

    class P:
        returncode = 0

    ok, info = watchdog.run_rotation(
        Path("/r"), None, tmp_path / "w" / "log.txt", runner=lambda *a, **k: P()
    )
    assert (
        ok and info == "rc=0" and (tmp_path / "w" / "log.txt").read_text().count("rotate exit") == 1
    )


def test_compose_messages() -> None:
    out = watchdog.Outcome(
        "auto-rotation-failed",
        "hop",
        {},
        [("same region", False, "rc=1"), ("LT-Siauliai", False, "rc=1")],
    )
    subj, body = watchdog.compose(out, DOWN_EXIT, "1.2.3.4", "5.6.7.8", Path("/log"))
    assert "FAILED" in subj and "LT-Siauliai: FAILED" in body and "rotate exit --region" in body
    out = watchdog.Outcome("auto-rotated", "hop", {}, [("same region", True, "rc=0")])
    subj, _ = watchdog.compose(out, DOWN_EXIT, "9.9.9.9", "5.6.7.8", None)
    assert "recovered" in subj


def test_rotation_in_progress_lock(tmp_path: Path) -> None:
    import fcntl

    assert watchdog.rotation_in_progress(tmp_path) in (True, False)  # depends on pgrep on the host
    fh = open(tmp_path / watchdog.LOCK_FILE, "a+")
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert watchdog.rotation_in_progress(tmp_path) is True
    fcntl.flock(fh, fcntl.LOCK_UN)
