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


def panel_pass(cfg: dict[str, Any]) -> str:
    v = os.environ.get("PROXY_PIPELINE_PANEL_PASS") or str(cfg.get("panel", {}).get("pass", ""))
    return v


def _telegram_section(cfg: dict[str, Any]) -> dict[str, Any]:
    sec = cfg.get("telegram")
    return sec if isinstance(sec, dict) else {}


def telegram_bot_token(cfg: dict[str, Any]) -> str:
    return (
        os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        or str(_telegram_section(cfg).get("bot_token") or "").strip()
    )


def telegram_notify_chat_id(cfg: dict[str, Any]) -> str:
    raw = os.environ.get("TELEGRAM_NOTIFY_CHAT_ID") or _telegram_section(cfg).get("notify_chat_id")
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
