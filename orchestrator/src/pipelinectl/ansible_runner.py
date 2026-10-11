from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml


def write_inventory(
    path: Path,
    exit_ip: str,
    exit_user: str,
    exit_key: Path,
    bridge_ip: str | None,
    bridge_user: str,
    bridge_key: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "[exit]",
        f"exit_node ansible_host={exit_ip} ansible_user={exit_user} ansible_ssh_private_key_file={exit_key}",
        "",
    ]
    if bridge_ip:
        lines += [
            "[bridge]",
            f"bridge_node ansible_host={bridge_ip} ansible_user={bridge_user} ansible_ssh_private_key_file={bridge_key}",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_extra_vars(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, default_flow_style=False), encoding="utf-8")


def run_playbook(
    ansible_dir: Path,
    playbook: Path,
    inventory: Path,
    extra_vars: dict[str, Any],
    limit: str | None = None,
) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
        yaml.safe_dump(extra_vars, f, default_flow_style=False)
        extra_path = f.name
    try:
        cmd = [
            "ansible-playbook",
            str(playbook.resolve()),
            "-i",
            str(inventory.resolve()),
            "-e",
            f"@{extra_path}",
        ]
        if limit:
            cmd += ["--limit", limit]
        env = os.environ.copy()
        env["ANSIBLE_CONFIG"] = str((ansible_dir / "ansible.cfg").resolve())
        subprocess.run(cmd, cwd=str(ansible_dir), env=env, check=True)
    finally:
        os.unlink(extra_path)
