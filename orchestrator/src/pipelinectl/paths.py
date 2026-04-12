from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    env = os.environ.get("PROXY_PIPELINE_ROOT")
    if env:
        return Path(env).resolve()
    # .../orchestrator/src/pipelinectl/paths.py -> parents[3] == repo root
    here = Path(__file__).resolve()
    return here.parents[3]


def terraform_dir(cfg: dict | None = None) -> Path:
    rel = "terraform"
    if cfg and cfg.get("paths", {}).get("terraform_dir"):
        rel = str(cfg["paths"]["terraform_dir"])
    return repo_root() / rel


def ansible_dir(cfg: dict | None = None) -> Path:
    rel = "ansible"
    if cfg and cfg.get("paths", {}).get("ansible_dir"):
        rel = str(cfg["paths"]["ansible_dir"])
    return repo_root() / rel


def state_dir(cfg: dict | None = None) -> Path:
    rel = "state"
    if cfg and cfg.get("paths", {}).get("state_dir"):
        rel = str(cfg["paths"]["state_dir"])
    p = repo_root() / rel
    p.mkdir(parents=True, exist_ok=True)
    return p
