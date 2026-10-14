from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from pipelinectl.paths import repo_root


def load_pipeline_config() -> dict[str, Any]:
    root = repo_root()
    path = root / "config" / "pipeline.yaml"
    if not path.exists():
        example = root / "config" / "pipeline.example.yaml"
        if example.exists():
            return yaml.safe_load(example.read_text(encoding="utf-8")) or {}
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def expand_path(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


def ssh_key_for_exit(cfg: dict[str, Any]) -> Path:
    ssh = cfg.get("ssh", {})
    return expand_path(str(ssh.get("exit_private_key_path", "~/.ssh/proxy_pipeline")))


def ssh_key_for_bridge(cfg: dict[str, Any]) -> Path:
    ssh = cfg.get("ssh", {})
    return expand_path(str(ssh.get("bridge_private_key_path", "~/.ssh/proxy_pipeline")))


def exit_ssh_user(cfg: dict[str, Any]) -> str:
    return str(cfg.get("ssh", {}).get("exit_user", "root"))


def bridge_ssh_user(cfg: dict[str, Any]) -> str:
    return str(cfg.get("ssh", {}).get("bridge_user", "ubuntu"))


def manual_bridge(cfg: dict[str, Any]) -> dict[str, str]:
    """RU bridge outside Terraform (`bridge_manual` in config): {"ip", "ssh_user"}, or {}."""
    section = cfg.get("bridge_manual") or {}
    if not isinstance(section, dict) or not str(section.get("ip") or "").strip():
        return {}
    return {"ip": str(section["ip"]).strip(), "ssh_user": str(section.get("ssh_user") or "root").strip()}


def panel_pass(cfg: dict[str, Any]) -> str:
    v = os.environ.get("PROXY_PIPELINE_PANEL_PASS") or str(cfg.get("panel", {}).get("pass", ""))
    return v


def _telegram_section(cfg: dict[str, Any]) -> dict[str, Any]:
    sec = cfg.get("telegram")
    return sec if isinstance(sec, dict) else {}


def _alerts_section(cfg: dict[str, Any]) -> dict[str, Any]:
    sec = cfg.get("alerts")
    return sec if isinstance(sec, dict) else {}


def telegram_bot_token(cfg: dict[str, Any]) -> str:
    """env TELEGRAM_BOT_TOKEN, then telegram.bot_token, then alerts.telegram_bot_token."""
    return (
        os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        or str(_telegram_section(cfg).get("bot_token") or "").strip()
        or str(_alerts_section(cfg).get("telegram_bot_token") or "").strip()
    )


def telegram_notify_chat_id(cfg: dict[str, Any]) -> str:
    """env TELEGRAM_NOTIFY_CHAT_ID, then telegram.notify_chat_id, then alerts.telegram_chat_id."""
    raw = (
        os.environ.get("TELEGRAM_NOTIFY_CHAT_ID")
        or _telegram_section(cfg).get("notify_chat_id")
        or _alerts_section(cfg).get("telegram_chat_id")
    )
    return str(raw or "").strip()


def telegram_allowed_user_ids(cfg: dict[str, Any]) -> list[int]:
    raw = _telegram_section(cfg).get("allowed_user_ids") or []
    out: list[int] = []
    if not isinstance(raw, list):
        return out
    for x in raw:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def alerts_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """alerts: section with secrets overridable from the environment."""
    a = dict(cfg.get("alerts") or {})
    for key, env in (
        ("smtp_pass", "PROXY_PIPELINE_SMTP_PASS"),
        ("sms_ru_api_id", "PROXY_PIPELINE_SMSRU_API_ID"),
    ):
        v = os.environ.get(env, "").strip()
        if v:
            a[key] = v
    return a
