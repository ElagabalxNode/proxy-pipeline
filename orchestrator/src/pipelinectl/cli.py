from __future__ import annotations

import time

import typer

from pipelinectl import ansible_runner, config, health, links, paths, terraform

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


@app.command()
def configure() -> None:
    """Write Ansible inventory from Terraform outputs and run playbooks."""
    cfg = _cfg()
    tf_dir = paths.terraform_dir(cfg)
    adir = paths.ansible_dir(cfg)
    paths.state_dir(cfg)
    outs = terraform.terraform_output_json(tf_dir)
    exit_ip = outs.get("exit_ip") or ""
    if not exit_ip:
        typer.secho("terraform output exit_ip is empty — apply Terraform first.", fg=typer.colors.RED)
        raise typer.Exit(1)
    bridge_ip = (outs.get("bridge_ip") or "").strip() or None
    bridge_user_tf = (outs.get("bridge_ssh_user") or "").strip()
    bridge_user = bridge_user_tf or config.bridge_ssh_user(cfg)

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

    panel = cfg.get("panel", {})
    xray = cfg.get("xray", {})
    extra = {
        "panel_user": str(panel.get("user", "admin")),
        "panel_pass": config.panel_pass(cfg),
        "panel_port": str(panel.get("port", "2053")),
        "panel_path": str(panel.get("path", "/secretpanel")),
        "reality_sni": str(xray.get("reality_sni", "www.microsoft.com")),
        "reality_port": int(xray.get("reality_port", 443)),
        "ss_port": int(xray.get("ss_port", 8388)),
        "ss_method": str(xray.get("ss_method", "2022-blake3-aes-128-gcm")),
        "chain_port": int(xray.get("chain_port", 8443)),
    }
    if not extra["panel_pass"]:
        typer.secho("panel_pass missing: set PROXY_PIPELINE_PANEL_PASS or config/pipeline.yaml", fg=typer.colors.RED)
        raise typer.Exit(1)

    playbook = (
        adir / "playbooks" / "site.yml" if bridge_ip else adir / "playbooks" / "exit_only.yml"
    )
    typer.echo(f"[configure] playbook -> {playbook.name}")
    ansible_runner.run_playbook(adir, playbook, inv, extra)


@app.command("validate")
def validate_cmd(
    fmt: str = typer.Option("text", "--format", help="text or json"),
) -> None:
    """Run health checks (SSH, TCP ports, optional bridge chain)."""
    cfg = _cfg()
    st = paths.state_dir(cfg)
    data = health.load_runtime(st)
    bridge_key = config.ssh_key_for_bridge(cfg) if data.get("bridge_ip") else None
    rpt = health.run_health(st, config.ssh_key_for_exit(cfg), bridge_key, config.bridge_ssh_user(cfg))
    if fmt == "json":
        typer.echo(__import__("json").dumps(rpt.to_json(), indent=2))
    else:
        for c in rpt.results:
            sym = "OK" if c.ok else ("WARN" if c.severity == "warn" else "FAIL")
            typer.echo(f"[{sym}] {c.name}: {c.detail}".rstrip())
        typer.echo(f"errors={rpt.errors()} warnings={rpt.warnings()}")
    raise typer.Exit(rpt.errors())


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
    target: str = typer.Argument(..., case_sensitive=False, help="exit | bridge | all"),
) -> None:
    """Recreate Exit and/or Bridge VMs via Terraform, then re-run configure."""
    cfg = _cfg()
    tf_dir = paths.terraform_dir(cfg)
    t = target.lower().strip()
    if t == "exit":
        terraform.terraform_destroy_targets(
            tf_dir, ["module.exit.cherryservers_server.exit"], auto_approve=True
        )
        terraform.terraform_apply(tf_dir)
    elif t == "bridge":
        outs = terraform.terraform_output_json(tf_dir)
        if not (outs.get("bridge_instance_id") or "").strip():
            typer.secho("Bridge not enabled in Terraform (enable_yandex_bridge=false).", fg=typer.colors.RED)
            raise typer.Exit(1)
        terraform.terraform_destroy_targets(
            tf_dir, ["module.bridge[0].yandex_compute_instance.bridge"], auto_approve=True
        )
        terraform.terraform_apply(tf_dir)
    elif t == "all":
        terraform.terraform_destroy_targets(
            tf_dir, ["module.exit.cherryservers_server.exit"], auto_approve=True
        )
        terraform.terraform_destroy_targets(
            tf_dir,
            ["module.bridge[0].yandex_compute_instance.bridge"],
            auto_approve=True,
            ignore_errors=True,
        )
        terraform.terraform_apply(tf_dir)
    else:
        typer.secho("target must be exit|bridge|all", fg=typer.colors.RED)
        raise typer.Exit(2)
    typer.echo("[rotate] waiting for cloud-init / SSH …")
    time.sleep(25)
    configure()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
