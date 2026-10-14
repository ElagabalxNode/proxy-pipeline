"""Alert when the chain stops carrying traffic — over channels that work from Russia WITHOUT the
VPN: e-mail and SMS (sms.ru). Telegram is sent too when configured, but it is not the lifeline.

Source of truth is the bridge chain-probe log mirrored into state/probe/ (one round a minute):
  DOWN     the last `fail_rounds` rounds all failed chain_http (or entry_https)
  STALE    no round for `stale_minutes` — probe, pull timer or the bridge itself is gone
  RECOVER  a healthy round after a DOWN/STALE alert
State (what was last announced) lives in state/alert_state.json so each transition is sent once.
"""

from __future__ import annotations

import json
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from pipelinectl import chainlog

STATE_FILE = "alert_state.json"


@dataclass
class Verdict:
    status: str  # "ok" | "down" | "stale"
    detail: str
    since: datetime | None = None


def evaluate(
    records: list[dict[str, Any]],
    now: datetime,
    fail_rounds: int = 3,
    stale_minutes: int = 15,
) -> Verdict:
    rounds: dict[datetime, set[str]] = {}
    for r in records:
        rounds.setdefault(r["_ts"], set())
        if r.get("ok") is not True:
            rounds[r["_ts"]].add(str(r["check"]))
    if not rounds:
        return Verdict("stale", "no probe rounds at all in the window")
    times = sorted(rounds)
    if now - times[-1] > timedelta(minutes=stale_minutes):
        return Verdict(
            "stale",
            f"last probe round {times[-1]:%Y-%m-%d %H:%M}Z — probe, pull timer or bridge down",
            times[-1],
        )
    # entry_https: clients cannot reach the bridge at all, which is as much an outage as a dead
    # chain. Its verdict names the bridge, so the watchdog alerts without rotating the Exit.
    # tls_exit alone is not an outage: the bare handshake probe fails while traffic still flows.
    outage = {"chain_http", "entry_https"}
    recent = times[-fail_rounds:]
    if len(recent) == fail_rounds and all(outage & rounds[t] for t in recent):
        # walk back to the start of the failing streak
        start = recent[0]
        for t in reversed(times[:-fail_rounds]):
            if outage & rounds[t]:
                start = t
            else:
                break
        return Verdict("down", chainlog.verdict(rounds[recent[-1]]), start)
    return Verdict("ok", "chain carries traffic")


def load_state(state_dir: Path) -> dict[str, Any]:
    p = state_dir / STATE_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state_dir: Path, state: dict[str, Any]) -> None:
    (state_dir / STATE_FILE).write_text(json.dumps(state, indent=2), encoding="utf-8")


def decide(prev: dict[str, Any], v: Verdict, now: datetime) -> tuple[str | None, dict[str, Any]]:
    """(message kind to send or None, new state). Transitions only; DOWN reminders every 6 h."""
    was = prev.get("status", "ok")
    new = {
        "status": v.status,
        "detail": v.detail,
        "since": v.since.isoformat() if v.since else None,
        "last_sent": prev.get("last_sent"),
        "updated": now.isoformat(),
    }
    if v.status != was:
        new["last_sent"] = now.isoformat()
        return ("recovered" if v.status == "ok" else v.status), new
    if v.status != "ok" and prev.get("last_sent"):
        last = datetime.fromisoformat(prev["last_sent"])
        if now - last >= timedelta(hours=6):
            new["last_sent"] = now.isoformat()
            return f"{v.status}-reminder", new
    return None, new


def compose(kind: str, v: Verdict, exit_ip: str, bridge_ip: str) -> tuple[str, str]:
    since = f" since {v.since:%Y-%m-%d %H:%M}Z" if v.since else ""
    if kind.startswith("down"):
        subj = f"[proxy-pipeline] DOWN{' (reminder)' if 'reminder' in kind else ''}: {v.detail}"
        body = (
            f"The chain does not carry traffic{since}.\nVerdict: {v.detail}\n"
            f"Exit: {exit_ip}   Bridge: {bridge_ip}\n\n"
            "Check the provider's abuse/status mail first, then on the controller:\n"
            "  pipelinectl chainlog --since 2h\n  pipelinectl validate --deep\n  pipelinectl rotate exit   # new Exit; QR unchanged\n"
        )
    elif kind.startswith("stale"):
        subj = "[proxy-pipeline] NO DATA from the bridge probe"
        body = (
            f"{v.detail}\nExit: {exit_ip}   Bridge: {bridge_ip}\n\n"
            "Either the bridge VM is down, or the probe/pull timers stopped:\n"
            "  systemctl status proxy-pipeline-probe-pull.timer   (controller)\n"
            "  pipelinectl validate --deep\n"
        )
    else:
        subj = "[proxy-pipeline] RECOVERED: chain carries traffic again"
        body = f"Exit: {exit_ip}   Bridge: {bridge_ip}\n"
    return subj, body


def send_email(cfg: dict[str, Any], subject: str, body: str) -> str:
    """Return '' on success, else the error. Needs alerts.email_to/smtp_host/smtp_user/smtp_pass."""
    a = cfg.get("alerts") or {}
    to, host = str(a.get("email_to") or ""), str(a.get("smtp_host") or "")
    user, pw = str(a.get("smtp_user") or ""), str(a.get("smtp_pass") or "")
    if not (to and host and user and pw):
        return "email not configured (alerts.email_to/smtp_host/smtp_user/smtp_pass)"
    port = int(a.get("smtp_port") or 465)
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = str(a.get("email_from") or user), to, subject
    msg.set_content(body)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(
                host, port, timeout=30, context=ssl.create_default_context()
            ) as s:
                s.login(user, pw)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(user, pw)
                s.send_message(msg)
        return ""
    except (OSError, smtplib.SMTPException) as e:
        return f"email: {e}"


def send_sms(cfg: dict[str, Any], text: str) -> str:
    """sms.ru (reachable from RU, no VPN). Needs alerts.sms_ru_api_id and alerts.sms_to."""
    a = cfg.get("alerts") or {}
    api_id, to = str(a.get("sms_ru_api_id") or ""), str(a.get("sms_to") or "")
    if not (api_id and to):
        return "sms not configured (alerts.sms_ru_api_id/sms_to)"
    q = urllib.parse.urlencode({"api_id": api_id, "to": to, "msg": text[:600], "json": 1})
    try:
        with urllib.request.urlopen("https://sms.ru/sms/send?" + q, timeout=30) as r:
            d = json.load(r)
        if d.get("status") != "OK":
            return f"sms: {d}"
        return ""
    except (OSError, json.JSONDecodeError) as e:
        return f"sms: {e}"


def send_telegram(token: str, chat_id: str, text: str) -> str:
    """Plain Bot API over HTTPS (no python-telegram-bot needed). '' on success, else the error.

    Sent from the controller, so it works whenever the controller has internet — but the
    recipient in Russia may not be able to READ it without the VPN; e-mail/SMS are the lifeline.
    """
    if not (token and chat_id):
        return "telegram not configured (alerts.telegram_bot_token / telegram_chat_id)"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text[:4000]}).encode()
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data=body, timeout=30
        ) as r:
            d = json.load(r)
        return "" if d.get("ok") else f"telegram: {d}"
    except urllib.error.HTTPError as e:
        return f"telegram: HTTP {e.code} {(e.read() or b'').decode()[:200]}"
    except (OSError, json.JSONDecodeError) as e:
        return f"telegram: {e}"
