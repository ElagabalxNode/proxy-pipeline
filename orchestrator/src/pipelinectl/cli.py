from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import typer

from pipelinectl import (
    alerts,
    ansible_runner,
    chainlog,
    cherry,
    config,
    deep_health,
    health,
    iprep,
    links,
    paths,
    probe,
    terraform,
    watchdog,
)

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
    bridge_sni: str | None = None,
    bridge_dest: str | None = None,
    exit_firewall_only: bool = False,
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
        typer.secho(
            "terraform output exit_ip is empty — apply Terraform first.", fg=typer.colors.RED
        )
        raise typer.Exit(1)
    bridge_ip = (bridge_ip_override or "").strip() or (outs.get("bridge_ip") or "").strip() or None
    bridge_user_tf = (outs.get("bridge_ssh_user") or "").strip()
    bridge_user = (
        (bridge_user_override or "").strip() or bridge_user_tf or config.bridge_ssh_user(cfg)
    )
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
    # The bridge dials its Reality dest on every client handshake — it must be fast from the
    # bridge's network. --bridge-sni changes the client QR (sni=); --bridge-dest alone does not.
    overrides: dict[str, str] = {}
    if bridge_sni:
        overrides["bridge_reality_sni_override"] = bridge_sni.strip()
        typer.secho(
            f"[configure] bridge Reality SNI -> {bridge_sni}: the client QR WILL change (sni=)",
            fg=typer.colors.YELLOW,
        )
    if bridge_dest:
        overrides["bridge_reality_dest_override"] = bridge_dest.strip()
        typer.echo(f"[configure] bridge Reality dest -> {bridge_dest} (QR unchanged)")

    if exit_firewall_only:
        if not bridge_ip:
            typer.secho(
                "--exit-firewall-only: terraform output bridge_ip is empty — refusing (would lock the bridge out).",
                fg=typer.colors.RED,
            )
            raise typer.Exit(1)
        playbook = adir / "playbooks" / "exit_firewall.yml"
        xray = cfg.get("xray", {})
        typer.echo(
            f"[configure] exit-firewall-only: {playbook.name} (inbounds NOT touched)  bridge={bridge_ip}"
        )
        ansible_runner.run_playbook(
            adir,
            playbook,
            inv,
            {
                "exit_fw_bridge_ip": bridge_ip,
                "reality_port": int(xray.get("reality_port", 443)),
                "ss_port": int(xray.get("ss_port", 8388)),
                "chain_port": int(xray.get("chain_port", 8443)),
                "panel_port": str(cfg.get("panel", {}).get("port", "2053")),
            },
            limit="exit",
        )
        return

    if bridge_only:
        if not bridge_ip:
            typer.secho(
                "--bridge-only set but terraform output bridge_ip is empty.", fg=typer.colors.RED
            )
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
            {
                "bridge_chain_mode": chain_mode,
                "bridge_reality_port": bridge_reality_port,
                **overrides,
            },
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
        "exit_fw_bridge_ip": bridge_ip or "",  # exit_firewall allow-list (empty = no bridge)
        **overrides,
    }
    if not extra["panel_pass"]:
        typer.secho(
            "panel_pass missing: set PROXY_PIPELINE_PANEL_PASS or config/pipeline.yaml",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    playbook = (
        adir / "playbooks" / "site.yml" if bridge_ip else adir / "playbooks" / "exit_only.yml"
    )
    typer.echo(f"[configure] playbook -> {playbook.name}")
    ansible_runner.run_playbook(adir, playbook, inv, extra)


@app.command()
def configure(
    bridge_sni: str = typer.Option(
        None,
        "--bridge-sni",
        help="Bridge Reality SNI+dest (must be fast FROM the bridge); changes the client QR",
    ),
    bridge_dest: str = typer.Option(
        None,
        "--bridge-dest",
        help="Bridge Reality dest host:port only (e.g. a reachable edge IP); QR unchanged",
    ),
    bridge_only: bool = typer.Option(
        False,
        "--bridge-only",
        help="Only (re)configure the bridge via --limit; never touch the Exit",
    ),
    exit_firewall_only: bool = typer.Option(
        False,
        "--exit-firewall-only",
        help="Only (re)apply the Exit firewall (idempotent; inbounds untouched)",
    ),
    bridge_ip: str = typer.Option(
        None, "--bridge-ip", help="Override bridge IP (use when a -target apply left outputs stale)"
    ),
    bridge_user: str = typer.Option(
        None, "--bridge-user", help="Override bridge SSH user (Timeweb=root, Yandex=ubuntu)"
    ),
) -> None:
    """Write Ansible inventory from Terraform outputs and run playbooks."""
    _do_configure(
        bridge_only=bridge_only,
        bridge_ip_override=bridge_ip,
        bridge_user_override=bridge_user,
        bridge_sni=bridge_sni,
        bridge_dest=bridge_dest,
        exit_firewall_only=exit_firewall_only,
    )


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
        typer.secho(
            "terraform output exit_ip is empty — apply Terraform first.", fg=typer.colors.RED
        )
        raise typer.Exit(1)

    exit_user = config.exit_ssh_user(cfg)
    exit_key = config.ssh_key_for_exit(cfg)
    exit_region = (
        str(cfg.get("providers", {}).get("cherry", {}).get("region", "")).strip() or "unknown"
    )

    typer.echo(f"[probe-targets] probing {len(candidates)} targets from {exit_ip} …")

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
            f"[probe-targets] passed={report['candidates_passed']}/{report['candidates_tested']}"
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
    deep: bool = typer.Option(
        False,
        "--deep",
        help="Panel login, inbounds, TLS probe, egress IP, one chain-probe round on the bridge",
    ),
    fmt: str = typer.Option("text", "--format", help="text or json"),
) -> None:
    """Run health checks (SSH, TCP ports, optional bridge chain)."""
    cfg = _cfg()
    st = paths.state_dir(cfg)
    data = health.load_runtime(st)
    bridge_key = config.ssh_key_for_bridge(cfg) if data.get("bridge_ip") else None
    rpt = health.run_health(
        st, config.ssh_key_for_exit(cfg), bridge_key, config.bridge_ssh_user(cfg)
    )
    if deep:
        deep_rpt = deep_health.run_deep_health(
            data,
            config.ssh_key_for_exit(cfg),
            config.panel_pass(cfg),
            config.exit_ssh_user(cfg),
            bridge_ip=str(data.get("bridge_ip") or ""),
            bridge_user=str(data.get("bridge_ssh_user") or config.bridge_ssh_user(cfg)),
            bridge_key=bridge_key,
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
        f"[doctor] Links:    .client-links ({n_links} profiles), .client-links-qr/ ({n_qr} PNG)"
    )

    def _ts(p: Path) -> str:
        if not p.exists():
            return "missing"
        return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")

    rt = st / "runtime.json"
    br = st / "bridge_runtime.json"
    typer.echo(f"[doctor] State:    runtime.json ({_ts(rt)}), bridge_runtime.json ({_ts(br)})")


@app.command("notify")
def notify_cmd(
    message: str = typer.Argument(..., help="Message text to send to the alerts chat"),
) -> None:
    """Send one Telegram message (alerts.telegram_bot_token / telegram_chat_id or telegram: section)."""
    cfg = _cfg()
    token, chat_id = config.telegram_bot_token(cfg), config.telegram_notify_chat_id(cfg)
    if not token or not chat_id:
        typer.secho(
            "telegram bot token / chat id missing (alerts.telegram_bot_token + alerts.telegram_chat_id, "
            "or telegram.bot_token + telegram.notify_chat_id, or env TELEGRAM_BOT_TOKEN/TELEGRAM_NOTIFY_CHAT_ID)",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    err = alerts.send_telegram(token, chat_id, message)
    if err:
        typer.secho(f"[notify] {err}", fg=typer.colors.RED)
        raise typer.Exit(2)
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


def _watchdog(dry_run: bool, fail_rounds: int, stale_minutes: int, rotate_enabled: bool) -> None:
    cfg = _cfg()
    st = paths.state_dir(cfg)
    root = paths.repo_root()
    now = datetime.now(timezone.utc)
    records = chainlog.load_records(
        st / "probe", now - timedelta(minutes=max(stale_minutes, fail_rounds) + 30)
    )
    verdict = alerts.evaluate(records, now, fail_rounds=fail_rounds, stale_minutes=stale_minutes)
    prev = alerts.load_state(st)
    rails = watchdog.rails_from_config(cfg)
    data = health.load_runtime(st)
    bridge_ip = str(data.get("bridge_ip") or "")
    bridge_user = str(data.get("bridge_ssh_user") or config.bridge_ssh_user(cfg))
    bridge_key = config.ssh_key_for_bridge(cfg)
    log_path = st / "watchdog" / f"rotate-{now:%Y%m%d}.log"
    typer.echo(
        f"[watchdog] status={verdict.status} ({verdict.detail}) previous={prev.get('status', 'ok')}"
    )

    def live_check() -> tuple[bool, str, list[dict]]:
        if not bridge_ip:
            return False, "no bridge_ip in state", []
        return deep_health._bridge_probe_round(bridge_ip, bridge_user, bridge_key)

    def rotate(region: str | None) -> tuple[bool, str]:
        if dry_run:
            return False, "dry-run: not rotating"
        typer.echo(f"[watchdog] rotating the Exit ({region or 'same region'}) … log: {log_path}")
        return watchdog.run_rotation(root, region, log_path)

    outcome = watchdog.run(
        verdict,
        prev,
        now,
        rails,
        live_check=live_check,
        rotate=rotate,
        in_progress=lambda: watchdog.rotation_in_progress(st),
        rotate_enabled=rotate_enabled and not dry_run,
    )
    typer.echo(f"[watchdog] -> {outcome.kind or 'nothing to send'}: {outcome.detail}")
    for region, ok, info in outcome.attempts:
        typer.echo(f"  rotation {region}: {'OK' if ok else 'FAILED'} ({info})")
    if outcome.kind is None:
        if not dry_run:
            alerts.save_state(st, outcome.state)
        return
    data = health.load_runtime(st)  # the Exit may have changed
    subject, body = watchdog.compose(
        outcome, verdict, str(data.get("exit_ip") or "?"), bridge_ip or "?", log_path
    )
    if dry_run:
        typer.echo(f"--- would send ---\n{subject}\n{body}")
        return
    acfg = {"alerts": config.alerts_config(cfg)}
    results = {
        "email": alerts.send_email(acfg, subject, body),
        "sms": alerts.send_sms(acfg, subject),
    }
    results["telegram"] = alerts.send_telegram(
        config.telegram_bot_token(cfg), config.telegram_notify_chat_id(cfg), f"{subject}\n{body}"
    )
    for ch, err in results.items():
        typer.echo(f"  {ch}: {'sent' if not err else err}")
    if all(results.values()) and outcome.kind != "auto-rotated":
        # nothing went out: do not record the transition, so the next run retries it
        typer.secho(
            "[watchdog] NO channel delivered — configure alerts.email_to/smtp_* or sms_ru_api_id/sms_to",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    alerts.save_state(st, outcome.state)


@app.command("watchdog")
def watchdog_cmd(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Evaluate and print; rotate nothing, send nothing"
    ),
    no_rotate: bool = typer.Option(
        False, "--no-rotate", help="Alert only, never rotate (= alert-check)"
    ),
    fail_rounds: int = typer.Option(
        3, "--fail-rounds", help="Consecutive failing probe rounds = DOWN"
    ),
    stale_minutes: int = typer.Option(
        15, "--stale-minutes", help="No probe round for this long = NO DATA"
    ),
) -> None:
    """Self-healing: chain DOWN because of the Exit → rotate the Exit (all gates), alert only if that fails.

    Runs from the proxy-pipeline-alert timer every 5 minutes. Rails: Exit-shaped verdicts only,
    live probe confirmation, cooldown + daily cap (config watchdog:), never during another rotation.
    """
    _watchdog(dry_run, fail_rounds, stale_minutes, rotate_enabled=not no_rotate)


@app.command("alert-check")
def alert_check_cmd(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Evaluate and print; send nothing, save nothing"
    ),
    fail_rounds: int = typer.Option(3, "--fail-rounds"),
    stale_minutes: int = typer.Option(15, "--stale-minutes"),
) -> None:
    """Alert-only evaluation of the probe log (watchdog without rotation)."""
    _watchdog(dry_run, fail_rounds, stale_minutes, rotate_enabled=False)


@app.command("chainlog")
def chainlog_cmd(
    since: str = typer.Option("24h", "--since", help="Look back: 90m | 24h | 7d"),
    pull: bool = typer.Option(
        False, "--pull", help="rsync the bridge log dir into state/probe first"
    ),
    windows: int = typer.Option(10, "--windows", help="How many failure windows to list"),
    fmt: str = typer.Option("text", "--format", help="text | json"),
) -> None:
    """Where did the chain stall? Summary of the bridge chain-probe log (state/probe/probe.jsonl).

    tls_exit = bridge→Exit hop, chain_http = whole chain, direct_http = bridge egress without the
    Exit. Each failure window gets a verdict from which checks failed together.
    """
    cfg = _cfg()
    st = paths.state_dir(cfg)
    log_dir = st / "probe"
    if pull:
        br_path = st / "bridge_runtime.json"
        if not br_path.exists():
            typer.secho(
                "state/bridge_runtime.json missing — configure the bridge first",
                fg=typer.colors.RED,
            )
            raise typer.Exit(1)
        br = json.loads(br_path.read_text(encoding="utf-8"))
        try:
            chainlog.pull(
                log_dir,
                str(br["bridge_ip"]),
                str(br.get("bridge_ssh_user") or "ubuntu"),
                config.ssh_key_for_bridge(cfg),
            )
        except (subprocess.CalledProcessError, OSError) as e:
            typer.secho(f"[chainlog] pull failed: {e}", fg=typer.colors.RED)
            raise typer.Exit(2) from e
    try:
        delta = chainlog.parse_since(since)
    except ValueError as e:
        typer.secho(str(e), fg=typer.colors.RED)
        raise typer.Exit(1) from e
    records = chainlog.load_records(log_dir, datetime.now(timezone.utc) - delta)
    if fmt == "json":
        wins = chainlog.failure_windows(records)
        out = {
            "since": since,
            "rounds": len({r["_ts"] for r in records}),
            "summary": chainlog.summarize(records),
            "windows": [
                {
                    "start": w.start.isoformat(),
                    "end": w.end.isoformat(),
                    "rounds": w.rounds,
                    "failed": dict(w.failed),
                    "verdict": w.verdict,
                }
                for w in wins
            ],
        }
        typer.echo(json.dumps(out, indent=2))
        return
    typer.echo(chainlog.render_text(records, since, max_windows=windows))


@app.command("ipcheck")
def ipcheck_cmd(
    ip: str = typer.Argument(..., help="IPv4 address to assess (e.g. a freshly created Exit)"),
    allow: str = typer.Option("", "--allow", help="Extra ports to treat as ours, comma-separated"),
    fmt: str = typer.Option("text", "--format", help="text | json"),
) -> None:
    """Is this IP dirty from a previous tenant? Shodan InternetDB view (no API key).

    Exit code 0 = clean, 1 = dirty (previous tenant's services / malware tags / foreign
    hostnames), 2 = could not check. `rotate` runs this on every new Exit and re-rolls on 1.
    """
    allowed = set(iprep.EXIT_PORTS)
    for p in filter(None, (x.strip() for x in allow.split(","))):
        allowed.add(int(p))
    data, err = iprep.fetch(ip)
    if err:
        typer.secho(f"{ip}: InternetDB unavailable ({err})", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(2)
    clean, reason = iprep.assess(data, allowed_ports=frozenset(allowed))
    if fmt == "json":
        typer.echo(
            json.dumps({"ip": ip, "clean": clean, "reason": reason, "internetdb": data}, indent=2)
        )
    else:
        typer.echo(f"{ip}: {'CLEAN' if clean else 'DIRTY'} — {reason}")
    raise typer.Exit(0 if clean else 1)


@app.command("stock")
def stock_cmd(
    plan: str = typer.Option(
        None, "--plan", help="Cherry plan slug (default: exit_plan from terraform.tfvars)"
    ),
    region: str = typer.Option(
        None, "--region", help="Only this region; with --format qty prints the number"
    ),
    all_plans: bool = typer.Option(
        False, "--all", help="Every cloud-VPS plan (B1-*/B2-*), not just the current"
    ),
    fmt: str = typer.Option("text", "--format", help="text | json | qty"),
) -> None:
    """Live Cherry stock per region — pick where to send `rotate exit --region` before running it.

    Stock is what Cherry reports right now; a region missing from a plan is not sold there
    (Gen-1 B1-* exists only in LT/NL/US/SG). `rotate` re-checks the chosen region itself and
    refuses when it is 0.
    """
    cfg = _cfg()
    tv = cherry.tfvars(cfg)
    token = cherry.api_token(cfg)
    if not token:
        typer.secho(
            "CHERRY_API_TOKEN not found (env, .env, or cherry_api_token in terraform.tfvars)",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    cur_plan, cur_region = tv.get("exit_plan", ""), tv.get("exit_region", "")
    plan = (plan or cur_plan).strip()
    if not plan and not all_plans:
        typer.secho(
            "no plan: pass --plan or set exit_plan in terraform.tfvars", fg=typer.colors.RED
        )
        raise typer.Exit(1)
    if fmt == "qty" and (not region or all_plans):
        typer.secho("--format qty needs --region and a single plan", fg=typer.colors.RED)
        raise typer.Exit(1)
    flt = cherry.is_cloud_vps if all_plans else (lambda s: s == plan)
    try:
        data = cherry.plans_stock(token, plan_filter=flt)
    except Exception as e:  # noqa: BLE001 — network/API failure must not look like "0 stock"
        typer.secho(f"[stock] Cherry API error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(3) from e

    if fmt == "qty":
        q = cherry.region_stock(data, plan, region)
        typer.echo("?" if q is None else str(q))
        return
    if fmt == "json":
        typer.echo(json.dumps(data, indent=2, sort_keys=True))
        return

    if not data:
        typer.secho(f"plan '{plan}' not found in the Cherry plan list", fg=typer.colors.YELLOW)
        raise typer.Exit(2)
    for pslug in sorted(data):
        regions = data[pslug]
        if region:
            regions = {r: q for r, q in regions.items() if r == region}
        typer.echo(f"\n{pslug}" + ("   <- current exit_plan" if pslug == cur_plan else ""))
        if not regions:
            typer.echo(f"  not sold in {region}" if region else "  no regions")
            continue
        typer.echo(f"  {'REGION':<16}{'STOCK':>7}")
        for r, q in sorted(regions.items(), key=lambda kv: (-kv[1], kv[0])):
            note = ("  <- current exit_region" if r == cur_region else "") + (
                "" if q > 0 else "  (out of stock)"
            )
            typer.echo(f"  {r:<16}{q:>7}{note}")
    if not all_plans and not region:
        ranked = [(q, r) for r, q in data.get(plan, {}).items() if q > 0 and r != cur_region]
        if ranked:
            best = max(ranked)[1]
            extra = f" --plan {plan}" if plan != cur_plan else ""
            typer.echo(f"\nbest alternative: pipelinectl rotate exit --region {best}{extra}")


@app.command()
def rotate(
    target: str = typer.Argument("all", case_sensitive=False, help="all | exit | bridge"),
    region: str = typer.Option(None, "--region", help="Set exit_region in tfvars before rotating"),
    plan: str = typer.Option(None, "--plan", help="Set exit_plan in tfvars before rotating"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="terraform apply -auto-approve (no prompt)"
    ),
    new_ip: bool = typer.Option(
        False, "--new-ip", help="bridge|all: replace the reserved bridge IP + identity (QR changes)"
    ),
) -> None:
    """Recreate Exit and/or Bridge and re-issue links, via scripts/rotate-chain.sh.

    The bridge keeps its reserved IP and identity, so the client QR survives every rotation
    unless --new-ip explicitly asks for a new address.

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
    if new_ip:
        cmd += ["--new-ip"]
    raise typer.Exit(subprocess.call(cmd, cwd=str(paths.repo_root())))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
