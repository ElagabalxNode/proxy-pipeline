"""Telegram bot: remote pipelinectl status / validate / rotate (optional [telegram] extra)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from pipelinectl import config, deep_health, health, links, paths

logger = logging.getLogger(__name__)


def _chunk_text(text: str, max_len: int) -> list[str]:
    text = text.strip()
    if len(text) <= max_len:
        return [text] if text else ["(empty)"]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + max_len])
        start += max_len
    return chunks


def _format_health_text(rpt: health.HealthReport) -> str:
    lines: list[str] = []
    for c in rpt.results:
        sym = "OK" if c.ok else ("WARN" if c.severity == "warn" else "FAIL")
        lines.append(f"[{sym}] {c.name}: {c.detail}".rstrip())
    lines.append(f"errors={rpt.errors()} warnings={rpt.warnings()}")
    return "\n".join(lines)


def _run_shallow_validate() -> str:
    cfg = config.load_pipeline_config()
    st = paths.state_dir(cfg)
    data = health.load_runtime(st)
    bridge_key = config.ssh_key_for_bridge(cfg) if data.get("bridge_ip") else None
    rpt = health.run_health(st, config.ssh_key_for_exit(cfg), bridge_key, config.bridge_ssh_user(cfg))
    return _format_health_text(rpt)


def _run_deep_validate() -> str:
    cfg = config.load_pipeline_config()
    st = paths.state_dir(cfg)
    data = health.load_runtime(st)
    bridge_key = config.ssh_key_for_bridge(cfg) if data.get("bridge_ip") else None
    rpt = health.run_health(st, config.ssh_key_for_exit(cfg), bridge_key, config.bridge_ssh_user(cfg))
    deep_rpt = deep_health.run_deep_health(
        data,
        config.ssh_key_for_exit(cfg),
        config.panel_pass(cfg),
        config.exit_ssh_user(cfg),
    )
    for c in deep_rpt.results:
        rpt.add(c)
    return _format_health_text(rpt)


def _run_doctor_text() -> str:
    cfg = config.load_pipeline_config()
    root = paths.repo_root()
    st = paths.state_dir(cfg)
    data = health.load_runtime(st)
    exit_ip = str(data.get("exit_ip") or "—")
    reality_port = int(data.get("reality_port", 443))
    sni = str(data.get("reality_sni") or "—")
    lines = [f"Exit:     {exit_ip}:{reality_port}  (Reality SNI: {sni})"]
    bridge_ip = str(data.get("bridge_ip") or "").strip()
    lines.append(f"Bridge:   {bridge_ip + ':443' if bridge_ip else '—'}")
    panel = str(data.get("panel_url") or "").strip()
    if not panel and exit_ip != "—":
        pport = str(data.get("panel_port") or "2053")
        ppath = str(data.get("panel_path") or "/secretpanel").strip().rstrip("/")
        prefix = f"/{ppath.lstrip('/')}" if ppath else ""
        panel = f"http://{exit_ip}:{pport}{prefix}/"
    lines.append(f"Panel:    {panel or '—'}")
    try:
        n_links = len(links.build_client_links_map(data))
    except Exception:  # noqa: BLE001
        n_links = 0
    qr_dir = root / ".client-links-qr"
    n_qr = len(list(qr_dir.glob("*.png"))) if qr_dir.is_dir() else 0
    lines.append(f"Links:    .client-links ({n_links} profiles), .client-links-qr/ ({n_qr} PNG)")
    from datetime import datetime

    def _ts(p: Path) -> str:
        if not p.exists():
            return "missing"
        return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")

    rt = st / "runtime.json"
    br = st / "bridge_runtime.json"
    lines.append(f"State:    runtime.json ({_ts(rt)}), bridge_runtime.json ({_ts(br)})")
    return "\n".join(lines)


def main() -> None:
    try:
        from telegram import Update
        from telegram.ext import Application, CommandHandler, ContextTypes, filters
    except ImportError as e:
        print("Install optional dependency: pip install -e '.[telegram]'", file=sys.stderr)
        raise SystemExit(2) from e

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        level=logging.INFO,
    )

    class AllowedUserFilter(filters.MessageFilter):
        def __init__(self, allowed_ids: frozenset[int]):
            super().__init__()
            self.allowed_ids = allowed_ids

        def filter(self, message):  # type: ignore[no-untyped-def]
            return bool(message.from_user and message.from_user.id in self.allowed_ids)

    cfg = config.load_pipeline_config()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or config.telegram_bot_token(cfg)
    allowed_raw = config.telegram_allowed_user_ids(cfg)
    allowed = frozenset(allowed_raw)

    if not token:
        print("Missing bot token: TELEGRAM_BOT_TOKEN or telegram.bot_token in config", file=sys.stderr)
        raise SystemExit(1)
    if not allowed:
        print("telegram.allowed_user_ids must be non-empty", file=sys.stderr)
        raise SystemExit(1)

    allowed_filter = AllowedUserFilter(allowed)

    async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert update.message
        text = await asyncio.to_thread(_run_doctor_text)
        await update.message.reply_text(text[:4096])

    async def validate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert update.message
        await update.message.reply_text("Running shallow validate…")
        text = await asyncio.to_thread(_run_shallow_validate)
        for chunk in _chunk_text(text, 4000):
            await update.message.reply_text(chunk)

    async def deep_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert update.message
        lock: asyncio.Lock = context.application.bot_data["lock"]
        if lock.locked():
            await update.message.reply_text("Another operation is running. Please wait.")
            return
        async with lock:
            await update.message.reply_text("Running deep validate (~30s)…")
            text = await asyncio.to_thread(_run_deep_validate)
            for chunk in _chunk_text(text, 4000):
                await update.message.reply_text(chunk)

    async def rotate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert update.message
        args = context.args or []
        if len(args) != 1 or args[0].lower() not in ("exit", "bridge", "all"):
            await update.message.reply_text("Usage: /rotate exit|bridge|all")
            return
        target = args[0].lower()
        lock: asyncio.Lock = context.application.bot_data["lock"]
        if lock.locked():
            await update.message.reply_text("Another operation is running. Please wait.")
            return
        root = paths.repo_root()
        env = os.environ.copy()
        env.setdefault("PYTHONPATH", str(root / "orchestrator" / "src"))
        async with lock:
            await update.message.reply_text(f"Starting rotate {target} (this may take several minutes)…")
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pipelinectl",
                "rotate",
                target,
                "--yes",  # nobody can answer the plan confirmation from a bot
                cwd=str(root),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out_b, _ = await proc.communicate()
            out = (out_b or b"").decode("utf-8", errors="replace")[-3500:]
            status = "OK" if proc.returncode == 0 else f"exit {proc.returncode}"
            await update.message.reply_text(f"rotate {target} {status}\n{out}"[:4096])

    async def links_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert update.message
        root = paths.repo_root()
        p = root / ".client-links"
        if not p.is_file():
            await update.message.reply_text("Missing .client-links — run pipelinectl links --export on the host.")
            return
        from telegram import InputFile

        await update.message.reply_document(document=InputFile(str(p)), filename=".client-links")

    async def qr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert update.message
        root = paths.repo_root()
        qr_dir = root / ".client-links-qr"
        pngs = sorted(qr_dir.glob("*.png")) if qr_dir.is_dir() else []
        if not pngs:
            await update.message.reply_text("No PNGs in .client-links-qr/ — run pipelinectl links --export --qr.")
            return
        from telegram import InputFile

        for path in pngs[:10]:
            await update.message.reply_photo(photo=InputFile(str(path)), filename=path.name)

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert update.message
        await update.message.reply_text(
            "/status — state summary (no network)\n"
            "/validate — shallow health (SSH, TCP, bridge chain)\n"
            "/deep — shallow + panel login, inbounds, egress IP, TLS\n"
            "/rotate exit|bridge|all — Terraform rotate + configure\n"
            "/links — send .client-links as a file\n"
            "/qr — send PNG QR files from .client-links-qr/\n"
            "/help — this text"
        )

    application = (
        Application.builder()
        .token(token)
        .concurrent_updates(False)
        .build()
    )
    application.bot_data["lock"] = asyncio.Lock()

    application.add_handler(CommandHandler("status", status_cmd, filters=allowed_filter))
    application.add_handler(CommandHandler("validate", validate_cmd, filters=allowed_filter))
    application.add_handler(CommandHandler("deep", deep_cmd, filters=allowed_filter))
    application.add_handler(CommandHandler("rotate", rotate_cmd, filters=allowed_filter))
    application.add_handler(CommandHandler("links", links_cmd, filters=allowed_filter))
    application.add_handler(CommandHandler("qr", qr_cmd, filters=allowed_filter))
    application.add_handler(CommandHandler("help", help_cmd, filters=allowed_filter))

    logger.info("pipelinebot polling (allowed users=%s)", json.dumps(list(allowed)))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
