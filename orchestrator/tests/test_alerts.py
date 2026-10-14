from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from pipelinectl import alerts

T0 = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def _recs(n_ok: int, n_fail: int, start: datetime = T0) -> list[dict]:
    out = []
    for i in range(n_ok + n_fail):
        ts = start + timedelta(minutes=i)
        failing = i >= n_ok
        for chk, ok in (
            ("tls_exit", not failing),
            ("chain_http", not failing),
            ("direct_http", True),
            ("dest_tls", True),
        ):
            out.append(
                {
                    "_ts": ts,
                    "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "check": chk,
                    "ok": ok,
                    "ms": 100,
                }
            )
    return out


def test_ok_when_recent_rounds_pass() -> None:
    v = alerts.evaluate(_recs(10, 0), T0 + timedelta(minutes=10))
    assert v.status == "ok"


def test_down_after_three_failing_rounds_with_streak_start() -> None:
    v = alerts.evaluate(_recs(5, 4), T0 + timedelta(minutes=9))
    assert v.status == "down" and "hop" in v.detail
    assert v.since == T0 + timedelta(minutes=5)  # first failing round, not just the last 3


def test_hop_handshake_failures_alone_are_not_an_outage() -> None:
    records = _recs(5, 4)
    for record in records:
        if record["check"] == "chain_http":
            record["ok"] = True
    assert alerts.evaluate(records, T0 + timedelta(minutes=9)).status == "ok"


def test_two_failing_rounds_is_not_yet_down() -> None:
    assert alerts.evaluate(_recs(5, 2), T0 + timedelta(minutes=7)).status == "ok"


def test_stale_when_no_recent_round() -> None:
    v = alerts.evaluate(_recs(5, 0), T0 + timedelta(minutes=40))
    assert v.status == "stale" and "12:04Z" in v.detail
    assert alerts.evaluate([], T0).status == "stale"


def test_decide_transitions_and_reminders() -> None:
    down = alerts.Verdict("down", "hop", T0)
    kind, st = alerts.decide({}, down, T0)
    assert kind == "down" and st["status"] == "down"
    kind, st2 = alerts.decide(st, down, T0 + timedelta(hours=1))
    assert kind is None and st2["last_sent"] == st["last_sent"]
    kind, st3 = alerts.decide(st2, down, T0 + timedelta(hours=7))
    assert kind == "down-reminder"
    kind, st4 = alerts.decide(st3, alerts.Verdict("ok", "fine"), T0 + timedelta(hours=8))
    assert kind == "recovered" and st4["status"] == "ok"
    kind, _ = alerts.decide(st4, alerts.Verdict("ok", "fine"), T0 + timedelta(hours=9))
    assert kind is None


def test_compose_mentions_action() -> None:
    subj, body = alerts.compose(
        "down", alerts.Verdict("down", "bridge→Exit hop", T0), "1.2.3.4", "5.6.7.8"
    )
    assert subj.startswith("[proxy-pipeline] DOWN") and "rotate exit" in body and "1.2.3.4" in body


def test_channels_report_not_configured() -> None:
    assert "not configured" in alerts.send_email({"alerts": {}}, "s", "b")
    assert "not configured" in alerts.send_sms({"alerts": {}}, "s")


def test_state_roundtrip(tmp_path) -> None:
    alerts.save_state(tmp_path, {"status": "down"})
    assert json.loads((tmp_path / alerts.STATE_FILE).read_text())["status"] == "down"
    assert alerts.load_state(tmp_path)["status"] == "down"
    assert alerts.load_state(tmp_path / "nope") == {}


def test_telegram_config_fallback_to_alerts_section(monkeypatch) -> None:
    from pipelinectl import config

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_NOTIFY_CHAT_ID", raising=False)
    cfg = {"alerts": {"telegram_bot_token": "t0k", "telegram_chat_id": "-100"}}
    assert config.telegram_bot_token(cfg) == "t0k" and config.telegram_notify_chat_id(cfg) == "-100"
    cfg2 = {
        "telegram": {"bot_token": "primary", "notify_chat_id": "1"},
        "alerts": {"telegram_bot_token": "t0k"},
    }
    assert config.telegram_bot_token(cfg2) == "primary"
    assert "not configured" in alerts.send_telegram("", "", "x")
