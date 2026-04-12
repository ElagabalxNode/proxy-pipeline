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
