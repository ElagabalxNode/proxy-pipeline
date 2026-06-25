from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import typer

from pipelinectl import ansible_runner, config, deep_health, health, links, paths, probe, terraform

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _cfg() -> dict:
    return config.load_pipeline_config()


@app.command()
def provision() -> None:
    """Run terraform init + apply in the repo terraform directory."""
    cfg = _cfg()
    tf_dir = paths.terraform_dir(cfg)
    typer.echo(f"[provision] terraform in {tf_dir}")
    terraform.terraform_init(tf_dir)
    terraform.terraform_apply(tf_dir)


def _bridge_chain_mode(tf_dir: Path) -> str:
    """Pick how the bridge relays to the Exit, by active provider.

    Timeweb's egress network breaks the xhttp/HTTP-2 chain to Exit:CHAIN_PORT (and vision
    must not be used on a relay hop), so a Timeweb bridge relays via Shadowsocks-2022 to
    the Exit's existing SS inbound (mode "ss", no Exit change). Yandex keeps "xhttp".
    Detected from Terraform state so it's automatic.
    """
    try:
        state = terraform.terraform_state_list(tf_dir)
    except Exception:  # noqa: BLE001
        return "xhttp"
    if any(a.endswith(".twc_server.bridge") for a in state):
        return "ss"
    return "xhttp"


def _do_configure(
    bridge_only: bool = False,
    bridge_ip_override: str | None = None,
    bridge_user_override: str | None = None,
) -> None:
    """Write inventory from Terraform outputs and run Ansible.

    bridge_only=True runs site.yml limited to the [bridge] group, so the Exit play
    has no hosts and is skipped — the live Exit is never touched. The bridge role
    reads all Exit details from state/runtime.json, so no extra_vars are needed.

    bridge_ip_override / bridge_user_override bypass the Terraform outputs for the
    bridge — useful right after a `-target` apply, whose root outputs are NOT
    refreshed (they still show the previously-active bridge).
    """
    cfg = _cfg()
    tf_dir = paths.terraform_dir(cfg)
    adir = paths.ansible_dir(cfg)
    st = paths.state_dir(cfg)
    outs = terraform.terraform_output_json(tf_dir)
    exit_ip = outs.get("exit_ip") or ""
    if not exit_ip:
        typer.secho("terraform output exit_ip is empty — apply Terraform first.", fg=typer.colors.RED)
        raise typer.Exit(1)
    bridge_ip = (bridge_ip_override or "").strip() or (outs.get("bridge_ip") or "").strip() or None
    bridge_user_tf = (outs.get("bridge_ssh_user") or "").strip()
    bridge_user = (bridge_user_override or "").strip() or bridge_user_tf or config.bridge_ssh_user(cfg)
    if bridge_ip_override:
        typer.echo(f"[configure] bridge IP override: {bridge_ip} (user={bridge_user})")

    inv = adir / "inventory" / "hosts.ini"
    ansible_runner.write_inventory(
        inv,
        exit_ip=exit_ip,
        exit_user=config.exit_ssh_user(cfg),
        exit_key=config.ssh_key_for_exit(cfg),
        bridge_ip=bridge_ip,
        bridge_user=bridge_user,
        bridge_key=config.ssh_key_for_bridge(cfg),
    )
    typer.echo(f"[configure] inventory -> {inv}")

    chain_mode = _bridge_chain_mode(tf_dir)
    # Timeweb's :443 gets its Reality/TLS reset by RU DPI (SS on a high port survives),
    # so the Timeweb bridge exposes VLESS+Reality on a non-443 port. Yandex stays on 443.
    bridge_reality_port = 8443 if chain_mode == "ss" else 443

    if bridge_only:
        if not bridge_ip:
            typer.secho("--bridge-only set but terraform output bridge_ip is empty.", fg=typer.colors.RED)
            raise typer.Exit(1)
        playbook = adir / "playbooks" / "site.yml"
        typer.echo(
            f"[configure] bridge-only: {playbook.name} --limit bridge (Exit NOT touched)"
            f"  chain_mode={chain_mode}  vless_port={bridge_reality_port}"
        )
        ansible_runner.run_playbook(
            adir,
            playbook,
            inv,
            {"bridge_chain_mode": chain_mode, "bridge_reality_port": bridge_reality_port},
            limit="bridge",
        )
        return

    # Load probed Reality target — mandatory, no silent fallback.
    try:
        rt_data = probe.load_reality_target(st)
    except (FileNotFoundError, ValueError) as e:
        typer.secho(f"[configure] {e}", fg=typer.colors.RED)
        raise typer.Exit(1) from e
    selected = rt_data["selected"]
    reality_sni = selected["sni"]
    reality_dest = selected["dest"]
    typer.echo(
        f"[configure] reality_target: sni={reality_sni}  dest={reality_dest}"
        f"  rtt={selected.get('measured_rtt_ms')}ms"
    )

    panel = cfg.get("panel", {})
    xray = cfg.get("xray", {})
    extra = {
        "panel_user": str(panel.get("user", "admin")),
        "panel_pass": config.panel_pass(cfg),
        "panel_port": str(panel.get("port", "2053")),
        "panel_path": str(panel.get("path", "/secretpanel")),
        "reality_sni": reality_sni,
        "reality_dest": reality_dest,
        "reality_port": int(xray.get("reality_port", 443)),
        "ss_port": int(xray.get("ss_port", 8388)),
        "ss_method": str(xray.get("ss_method", "2022-blake3-aes-128-gcm")),
        "chain_port": int(xray.get("chain_port", 8443)),
        "bridge_chain_mode": chain_mode,
        "bridge_reality_port": bridge_reality_port,
    }
    if not extra["panel_pass"]:
        typer.secho("panel_pass missing: set PROXY_PIPELINE_PANEL_PASS or config/pipeline.yaml", fg=typer.colors.RED)
        raise typer.Exit(1)

    playbook = (
        adir / "playbooks" / "site.yml" if bridge_ip else adir / "playbooks" / "exit_only.yml"
    )
    typer.echo(f"[configure] playbook -> {playbook.name}")
    ansible_runner.run_playbook(adir, playbook, inv, extra)


@app.command()
def configure(
    bridge_only: bool = typer.Option(
        False, "--bridge-only", help="Only (re)configure the bridge via --limit; never touch the Exit"
    ),
    bridge_ip: str = typer.Option(
        None, "--bridge-ip", help="Override bridge IP (use when a -target apply left outputs stale)"
    ),
    bridge_user: str = typer.Option(
        None, "--bridge-user", help="Override bridge SSH user (Timeweb=root, Yandex=ubuntu)"
    ),
) -> None:
    """Write Ansible inventory from Terraform outputs and run playbooks."""
    _do_configure(bridge_only=bridge_only, bridge_ip_override=bridge_ip, bridge_user_override=bridge_user)


@app.command("probe-targets")
def probe_targets_cmd(
    targets_file: Path = typer.Option(
        None,
        "--targets",
        help="Path to reality_targets.yaml (default: config/reality_targets.yaml)",
        exists=True,
    ),
    fmt: str = typer.Option("text", "--format", help="text or json"),
) -> None:
    """Probe Reality SNI candidates from Exit VPS and write state/reality_target.json."""
    cfg = _cfg()
    root = paths.repo_root()
    st = paths.state_dir(cfg)

    tf_path = targets_file or root / "config" / "reality_targets.yaml"
    if not tf_path.exists():
        typer.secho(f"Targets file not found: {tf_path}", fg=typer.colors.RED)
        raise typer.Exit(1)

    candidates = probe.load_target_candidates(tf_path)
    if not candidates:
        typer.secho("No enabled targets in targets file.", fg=typer.colors.RED)
        raise typer.Exit(1)

    # Get Exit IP from Terraform outputs (already applied before probe-targets).
    tf_dir = paths.terraform_dir(cfg)
    try:
        outs = terraform.terraform_output_json(tf_dir)
    except Exception as e:  # noqa: BLE001
        typer.secho(f"Failed to read Terraform outputs: {e}", fg=typer.colors.RED)
        raise typer.Exit(1) from e
    exit_ip = str(outs.get("exit_ip") or "").strip()
    if not exit_ip:
        typer.secho("terraform output exit_ip is empty — apply Terraform first.", fg=typer.colors.RED)
        raise typer.Exit(1)

    exit_user = config.exit_ssh_user(cfg)
    exit_key = config.ssh_key_for_exit(cfg)
    exit_region = str(cfg.get("providers", {}).get("cherry", {}).get("region", "")).strip() or "unknown"

    typer.echo(
        f"[probe-targets] probing {len(candidates)} targets from {exit_ip} …"
    )

    try:
        results = probe.probe_targets_on_exit(candidates, exit_ip, exit_user, exit_key)
    except RuntimeError as e:
        typer.secho(f"[probe-targets] {e}", fg=typer.colors.RED)
        raise typer.Exit(1) from e

    try:
        report = probe.pick_winner(results, exit_ip, exit_region)
    except RuntimeError as e:
        typer.secho(f"[probe-targets] {e}", fg=typer.colors.RED)
        # Still write a report so operator can inspect all_results
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        report = {
            "selected": None,
            "candidates_tested": len(results),
            "candidates_passed": 0,
            "all_results": results,
            "error": str(e),
            "timestamp": now,
        }
        out_path = probe.write_reality_target(st, report)
        typer.secho(f"[probe-targets] wrote failed report → {out_path}", fg=typer.colors.YELLOW)
        raise typer.Exit(1) from e

    out_path = probe.write_reality_target(st, report)
    sel = report["selected"]

    if fmt == "json":
        typer.echo(json.dumps(report, indent=2))
    else:
        typer.echo(
            f"[probe-targets] winner: {sel['id']}"
            f"  sni={sel['sni']}"
            f"  rtt={sel['measured_rtt_ms']}ms"
            f"  alpn={sel['alpn']}"
            f"  score={sel['score']}"
        )
        typer.echo(
            f"[probe-targets] passed={report['candidates_passed']}"
            f"/{report['candidates_tested']}"
        )
        typer.echo(f"[probe-targets] wrote → {out_path}")


def _print_health_report(rpt: health.HealthReport, fmt: str) -> None:
    if fmt == "json":
        typer.echo(json.dumps(rpt.to_json(), indent=2))
    else:
        for c in rpt.results:
            sym = "OK" if c.ok else ("WARN" if c.severity == "warn" else "FAIL")
            typer.echo(f"[{sym}] {c.name}: {c.detail}".rstrip())
        typer.echo(f"errors={rpt.errors()} warnings={rpt.warnings()}")


@app.command("validate")
def validate_cmd(
    deep: bool = typer.Option(False, "--deep", help="Panel login, inbounds, TLS probe, egress IP"),
    fmt: str = typer.Option("text", "--format", help="text or json"),
) -> None:
    """Run health checks (SSH, TCP ports, optional bridge chain)."""
    cfg = _cfg()
    st = paths.state_dir(cfg)
    data = health.load_runtime(st)
    bridge_key = config.ssh_key_for_bridge(cfg) if data.get("bridge_ip") else None
    rpt = health.run_health(st, config.ssh_key_for_exit(cfg), bridge_key, config.bridge_ssh_user(cfg))
    if deep:
        deep_rpt = deep_health.run_deep_health(
            data,
            config.ssh_key_for_exit(cfg),
            config.panel_pass(cfg),
            config.exit_ssh_user(cfg),
        )
        for c in deep_rpt.results:
            rpt.add(c)
    _print_health_report(rpt, fmt)
    raise typer.Exit(rpt.errors())


@app.command()
def doctor() -> None:
    """Show state summary from state/*.json without network checks."""
    cfg = _cfg()
    root = paths.repo_root()
    st = paths.state_dir(cfg)
    data = health.load_runtime(st)
    exit_ip = str(data.get("exit_ip") or "—")
    reality_port = int(data.get("reality_port", 443))
    sni = str(data.get("reality_sni") or "—")
    typer.echo(f"[doctor] Exit:     {exit_ip}:{reality_port}  (Reality SNI: {sni})")
    bridge_ip = str(data.get("bridge_ip") or "").strip()
    typer.echo(f"[doctor] Bridge:   {bridge_ip + ':443' if bridge_ip else '—'}")
    panel = str(data.get("panel_url") or "").strip()
    if not panel and exit_ip != "—":
        pport = str(data.get("panel_port") or "2053")
        ppath = str(data.get("panel_path") or "/secretpanel").rstrip("/") or ""
        prefix = f"/{ppath.lstrip('/')}" if ppath else ""
        panel = f"http://{exit_ip}:{pport}{prefix}/"
    typer.echo(f"[doctor] Panel:    {panel or '—'}")

    try:
        n_links = len(links.build_client_links_map(data))
    except Exception:  # noqa: BLE001
        n_links = 0
    qr_dir = root / ".client-links-qr"
    n_qr = len(list(qr_dir.glob("*.png"))) if qr_dir.is_dir() else 0
    typer.echo(
        f"[doctor] Links:    .client-links ({n_links} profiles), "
        f".client-links-qr/ ({n_qr} PNG)"
    )

    def _ts(p: Path) -> str:
        if not p.exists():
            return "missing"
        return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")

    rt = st / "runtime.json"
    br = st / "bridge_runtime.json"
    typer.echo(
        f"[doctor] State:    runtime.json ({_ts(rt)}), "
        f"bridge_runtime.json ({_ts(br)})"
    )


@app.command("notify")
def notify_cmd(
    message: str = typer.Argument(..., help="Message text to send to notify_chat_id"),
) -> None:
    """Send one Telegram message (requires [telegram] extra and notify_chat_id)."""
    try:
        from telegram import Bot
    except ImportError as e:
        typer.secho(
            "Install optional dependency: pip install -e '.[telegram]'",
            fg=typer.colors.RED,
        )
        raise typer.Exit(2) from e

    cfg = _cfg()
    token = config.telegram_bot_token(cfg)
    chat_id = config.telegram_notify_chat_id(cfg)
    if not token:
        typer.secho("telegram bot_token missing (TELEGRAM_BOT_TOKEN or config)", fg=typer.colors.RED)
        raise typer.Exit(1)
    if not chat_id:
        typer.secho("telegram notify_chat_id missing (config telegram.notify_chat_id)", fg=typer.colors.RED)
        raise typer.Exit(1)

    async def _send() -> None:
        bot = Bot(token)
        async with bot:
            await bot.send_message(chat_id=chat_id, text=message)

    asyncio.run(_send())
    typer.echo("[notify] sent")


@app.command("links")
def links_cmd(
    export: bool = typer.Option(True, "--export/--no-export", help="Write repo .client-links"),
    qr: bool = typer.Option(False, "--qr/--no-qr", help="Write PNG QR codes to .client-links-qr"),
    fmt: str = typer.Option("text", "--format", help="text or json"),
) -> None:
    """Print (and optionally write) client links from state/*.json."""
    cfg = _cfg()
    root = paths.repo_root()
    st = paths.state_dir(cfg)
    text = links.export_client_links(
        root,
        st,
        fmt=("json" if fmt == "json" else "text"),
        write_file=export and fmt != "json",
        write_qr=qr and fmt != "json",
    )
    typer.echo(text)
    if export and fmt != "json":
        typer.echo(f"[links] wrote {root / '.client-links'}")
    if qr and fmt != "json":
        typer.echo(f"[links] wrote {root / '.client-links-qr'}")


@app.command()
def rotate(
    target: str = typer.Argument("all", case_sensitive=False, help="all | exit | bridge"),
    region: str = typer.Option(None, "--region", help="Set exit_region in tfvars before rotating"),
    plan: str = typer.Option(None, "--plan", help="Set exit_plan in tfvars before rotating"),
    yes: bool = typer.Option(False, "--yes", "-y", help="terraform apply -auto-approve (no prompt)"),
) -> None:
    """Recreate Exit and/or Bridge and re-issue links, via scripts/rotate-chain.sh.

    Thin wrapper over the script so the robust flow lives in one place: venv activation,
    Yandex auth from config/key.json, refusing on stale Timeweb-in-state or a Gen-1 plan in
    a Gen-2-only region, `terraform apply -replace`, wait for SSH, re-probe Reality,
    configure, and links --qr. Default apply is interactive; pass --yes for unattended.
    """
    t = target.lower().strip()
    if t not in ("all", "exit", "bridge"):
        typer.secho("target must be all|exit|bridge", fg=typer.colors.RED)
        raise typer.Exit(2)
    script = paths.repo_root() / "scripts" / "rotate-chain.sh"
    if not script.exists():
        typer.secho(f"rotation script not found: {script}", fg=typer.colors.RED)
        raise typer.Exit(1)
    cmd = ["bash", str(script), t]
    if region:
        cmd += ["--region", region]
    if plan:
        cmd += ["--plan", plan]
    if yes:
        cmd += ["--yes"]
    raise typer.Exit(subprocess.call(cmd, cwd=str(paths.repo_root())))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
