"""Interactive menu (`pipelinectl menu`): the same commands, chosen by number.

Every action runs the ordinary `pipelinectl` command in a child process, so the menu adds no
logic of its own; it only shows the chain, asks before anything that costs money or changes a
node, and edits a few settings in config/pipeline.yaml without disturbing the rest of the file.
"""

from __future__ import annotations

import re
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pipelinectl import certs, cherry, config, health, paths

# (section, key, prompt, allowed values or None for free text)
SETTINGS: list[tuple[str, str, str, tuple[str, ...] | None]] = [
    ("watchdog", "auto_rotate", "Автозамена Exit при сбое", ("true", "false")),
    ("firewall", "direct_policy", "Прямые подключения к Exit", ("public", "allowlist", "closed")),
    ("firewall", "ssh_world", "SSH к Exit открыт всем", ("true", "false")),
    ("bridge_manual", "provider", "Провайдер моста (VDSina = ротация через API)", None),
    ("alerts", "telegram_chat_id", "Telegram: ID чата для оповещений", None),
]


def set_scalar(text: str, section: str, key: str, value: str) -> str:
    """Set `key` inside the top-level `section` of a YAML file, keeping comments and layout.

    Adds the key (or the whole section) when it is missing. Only plain scalars are handled.
    """
    rendered = value if re.fullmatch(r"true|false|-?\d+|[A-Za-z0-9_.:/-]+", value) else f'"{value}"'
    block = re.search(rf"(?ms)^{re.escape(section)}:[ \t]*(?:#[^\n]*)?\n((?:[ \t]+[^\n]*\n|[ \t]*\n)*)", text)
    if not block:
        return text.rstrip("\n") + f"\n\n{section}:\n  {key}: {rendered}\n"
    body = block.group(1)
    line = re.search(rf"(?m)^([ \t]+){re.escape(key)}:[ \t]*([^#\n]*?)([ \t]*#[^\n]*)?$", body)
    if line:
        new_body = body[: line.start(2)] + rendered + body[line.end(2):]
    else:
        new_body = f"  {key}: {rendered}\n" + body
    return text[: block.start(1)] + new_body + text[block.end(1):]


def overview(cfg: dict[str, Any], state_dir: Path, resolve: Callable[[str], str] = socket.gethostbyname) -> str:
    """The chain as it is recorded: nodes, providers, entry name, certificate, switches."""
    data = health.load_runtime(state_dir)
    tfvars = cherry.tfvars(cfg)
    manual = cfg.get("bridge_manual") if isinstance(cfg.get("bridge_manual"), dict) else {}
    domain = str(data.get("bridge_domain") or (cfg.get("bridge_entry") or {}).get("domain") or "")
    lines = [
        f"Exit    {data.get('exit_ip') or '—':<16} Cherry {tfvars.get('exit_region', '?')} {tfvars.get('exit_plan', '')}",
        f"Мост    {data.get('bridge_ip') or '—':<16} {manual.get('provider') or 'Terraform/нет'}"
        f" (в конфигурации: {config.manual_bridge(cfg).get('ip') or '—'})",
    ]
    if domain:
        try:
            answer = resolve(domain)
        except OSError as e:
            answer = f"не резолвится ({e})"
        same = "совпадает с мостом" if answer == data.get("bridge_ip") else "НЕ совпадает с мостом"
        lines.append(f"Домен   {domain} -> {answer} ({same})")
        try:
            cert, _ = certs.published(state_dir, certs.zone_of(cfg))
            info = certs.describe(cert)
            lines.append(f"Сертификат до {info.get('not_after') or '—'}" if info.get("present") else "Сертификат нет")
        except (OSError, ValueError, subprocess.SubprocessError):
            lines.append("Сертификат не прочитан")
    fw = cfg.get("firewall") if isinstance(cfg.get("firewall"), dict) else {}
    wd = cfg.get("watchdog") if isinstance(cfg.get("watchdog"), dict) else {}
    lines.append(f"Firewall Exit: direct={fw.get('direct_policy') or '—'}  ssh_world={fw.get('ssh_world')}"
                 f"   Автозамена Exit: {wd.get('auto_rotate', True)}")
    return "\n".join(lines)


def _run(args: list[str]) -> int:
    print(f"\n$ pipelinectl {' '.join(args)}")
    return subprocess.call([sys.executable, "-c", "from pipelinectl.cli import main; main()", *args])


def _ask(prompt: str, ask: Callable[[str], str]) -> str:
    try:
        return ask(prompt).strip()
    except EOFError:
        return "0"


def _yes(prompt: str, ask: Callable[[str], str]) -> bool:
    return _ask(f"{prompt} [y/N] ", ask).lower() in ("y", "yes", "д", "да")


def _rotate_exit(ask: Callable[[str], str], run: Callable[[list[str]], int]) -> None:
    run(["stock"])
    region = _ask("Регион (пусто = текущий): ", ask)
    if not _yes("Заменить Exit? Новый сервер Cherry оплачивается, старый удаляется", ask):
        return
    run(["rotate", "exit", "--yes", *(["--region", region] if region else [])])


def _rotate_bridge(ask: Callable[[str], str], run: Callable[[list[str]], int]) -> None:
    new_ip = _ask("IP нового сервера (пусто = заказать через API провайдера): ", ask)
    if new_ip:
        if _yes(f"Перенести мост на {new_ip}?", ask):
            run(["rotate", "bridge", "--bridge-ip", new_ip])
        return
    if run(["rotate", "bridge", "--dry-run"]) != 0:
        return
    if not _yes("Заказать сервер(ы) и перенести мост?", ask):
        return
    delete_old = _yes("Удалить старый сервер после успешного переноса?", ask)
    run(["rotate", "bridge", "--yes", *(["--delete-old"] if delete_old else [])])


def _settings(cfg_path: Path, ask: Callable[[str], str]) -> None:
    import yaml

    while True:
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
        print("\nНастройки (config/pipeline.yaml):")
        for i, (section, key, label, _) in enumerate(SETTINGS, 1):
            value = (cfg.get(section) or {}).get(key) if isinstance(cfg.get(section), dict) else None
            print(f"  {i}. {label}: {value if value not in (None, '') else '—'}")
        print("  0. Назад")
        choice = _ask("> ", ask)
        if not choice.isdigit() or not 1 <= int(choice) <= len(SETTINGS):
            return
        section, key, label, allowed = SETTINGS[int(choice) - 1]
        hint = f" ({'/'.join(allowed)})" if allowed else ""
        value = _ask(f"{label}{hint}: ", ask)
        if not value or (allowed and value not in allowed):
            print("не изменено")
            continue
        cfg_path.write_text(set_scalar(cfg_path.read_text(), section, key, value))
        print(f"{section}.{key} = {value}")


def run_menu(ask: Callable[[str], str] = input, run: Callable[[list[str]], int] = _run) -> None:
    cfg_path = paths.repo_root() / "config" / "pipeline.yaml"
    actions: list[tuple[str, Callable[[], object]]] = [
        ("Статус: какой узел сломан и чем чинить", lambda: run(["status"])),
        ("Цепочка: узлы, адреса, домен, сертификат", lambda: print(
            "\n" + overview(config.load_pipeline_config(), paths.state_dir(config.load_pipeline_config())))),
        ("Подробные проверки (validate --deep)", lambda: run(["validate", "--deep"])),
        ("История сбоев за сутки (chainlog)", lambda: run(["chainlog"])),
        ("Клиентские ссылки и QR", lambda: run(["links", "--qr"])),
        ("Заменить Exit", lambda: _rotate_exit(ask, run)),
        ("Заменить мост", lambda: _rotate_bridge(ask, run)),
        ("Перенастроить мост на месте (configure --bridge-only)",
         lambda: _yes("Применить конфигурацию к мосту?", ask) and run(["configure", "--bridge-only"])),
        ("DNS: показать записи", lambda: run(["dns", "show"])),
        ("Сертификат: продлить и доставить", lambda: run(["cert", "renew", "--deploy"])),
        ("Резервная копия", lambda: run(["backup"])),
        ("Настройки", lambda: _settings(cfg_path, ask)),
    ]
    while True:
        print("\n== proxy-pipeline ==")
        for i, (label, _) in enumerate(actions, 1):
            print(f"  {i:>2}. {label}")
        print("   0. Выход")
        choice = _ask("> ", ask)
        if choice in ("", "0", "q"):
            return
        if choice.isdigit() and 1 <= int(choice) <= len(actions):
            actions[int(choice) - 1][1]()
        else:
            print("нет такого пункта")
