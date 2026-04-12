from __future__ import annotations

import json
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CheckResult:
    name: str
    ok: bool
    severity: str  # "error" | "warn" | "info"
    detail: str = ""


@dataclass
class HealthReport:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, r: CheckResult) -> None:
        self.results.append(r)

    def errors(self) -> int:
        return sum(1 for r in self.results if not r.ok and r.severity == "error")

    def warnings(self) -> int:
        return sum(1 for r in self.results if r.severity == "warn")

    def to_json(self) -> dict[str, Any]:
        return {
            "errors": self.errors(),
            "warnings": self.warnings(),
            "checks": [
                {"name": r.name, "ok": r.ok, "severity": r.severity, "detail": r.detail}
                for r in self.results
            ],
        }


def _tcp_open(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ssh_ok(host: str, user: str, key: Path, extra_args: list[str] | None = None) -> tuple[bool, str]:
    cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=8",
        "-i",
        str(key),
        f"{user}@{host}",
        "echo ok",
    ]
    if extra_args:
        cmd[1:1] = extra_args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if p.returncode == 0 and "ok" in p.stdout:
            return True, ""
        return False, (p.stderr or p.stdout or "").strip()[:500]
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def load_runtime(state_dir: Path) -> dict[str, Any]:
    core = state_dir / "runtime.json"
    if not core.exists():
        return {}
    data = json.loads(core.read_text(encoding="utf-8"))
    bridge_path = state_dir / "bridge_runtime.json"
    if bridge_path.exists():
        data.update(json.loads(bridge_path.read_text(encoding="utf-8")))
    return data


def run_health(
    state_dir: Path,
    exit_key: Path,
    bridge_key: Path | None = None,
    bridge_user: str = "ubuntu",
) -> HealthReport:
    rpt = HealthReport()
    data = load_runtime(state_dir)
    exit_ip = data.get("exit_ip")
    if not exit_ip:
        rpt.add(CheckResult("exit_ip", False, "error", "state/runtime.json missing or empty exit_ip"))
        return rpt

    ok, msg = _ssh_ok(exit_ip, "root", exit_key)
    rpt.add(CheckResult("exit_ssh", ok, "error" if not ok else "info", msg))

    for label, port in (
        ("exit_reality", int(data.get("reality_port", 443))),
        ("exit_ss", int(data.get("ss_port", 8388))),
        ("exit_chain", int(data.get("chain_port", 8443))),
    ):
        open_ok = _tcp_open(exit_ip, port)
        rpt.add(CheckResult(f"tcp_{label}", open_ok, "error" if not open_ok else "info", f"{exit_ip}:{port}"))

    bridge_ip = data.get("bridge_ip")
    bridge_user = str(data.get("bridge_ssh_user") or bridge_user)
    if bridge_ip and bridge_key:
        ok_b, msg_b = _ssh_ok(bridge_ip, bridge_user, bridge_key, extra_args=[])
        rpt.add(CheckResult("bridge_ssh", ok_b, "error" if not ok_b else "info", msg_b))
        rpt.add(
            CheckResult(
                "tcp_bridge_443",
                _tcp_open(bridge_ip, 443),
                "error",
                f"{bridge_ip}:443",
            )
        )
        if ok_b:
            chain_port = int(data.get("chain_port", 8443))
            remote_cmd = f"timeout 6 bash -c 'echo >/dev/tcp/{exit_ip}/{chain_port}'"
            p = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-i",
                    str(bridge_key),
                    f"{bridge_user}@{bridge_ip}",
                    remote_cmd,
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            chain_ok = p.returncode == 0
            rpt.add(
                CheckResult(
                    "bridge_to_exit_chain",
                    chain_ok,
                    "error" if not chain_ok else "info",
                    (p.stderr or p.stdout or "").strip()[:300],
                )
            )
    elif bridge_ip and not bridge_key:
        rpt.add(CheckResult("bridge_ssh", False, "warn", "bridge_ip set but no bridge SSH key path"))

    return rpt
