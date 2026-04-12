from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


def _run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    subprocess.run(cmd, cwd=str(cwd), env=merged, check=True)


def terraform_init(tf_dir: Path) -> None:
    _run(["terraform", "init", "-input=false"], tf_dir)


def terraform_apply(tf_dir: Path, auto_approve: bool = True) -> None:
    cmd = ["terraform", "apply", "-input=false"]
    if auto_approve:
        cmd.append("-auto-approve")
    _run(cmd, tf_dir)


def terraform_destroy_targets(
    tf_dir: Path, targets: list[str], auto_approve: bool = True, ignore_errors: bool = False
) -> None:
    cmd = ["terraform", "destroy", "-input=false"]
    for t in targets:
        cmd += ["-target", t]
    if auto_approve:
        cmd.append("-auto-approve")
    if ignore_errors:
        merged = os.environ.copy()
        subprocess.run(cmd, cwd=str(tf_dir), env=merged, check=False)
    else:
        _run(cmd, tf_dir)


def terraform_output_json(tf_dir: Path) -> dict[str, Any]:
    proc = subprocess.run(
        ["terraform", "output", "-json"],
        cwd=str(tf_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    raw = json.loads(proc.stdout)
    return {k: v.get("value") for k, v in raw.items()}
