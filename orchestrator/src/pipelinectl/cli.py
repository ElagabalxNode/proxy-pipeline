from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import typer

from pipelinectl import (
    alerts,
    ansible_runner,
    backup,
    bridge_rotate,
    certs,
    chainlog,
    cherry,
    config,
    deep_health,
    health,
    inventory,
    iprep,
    links,
    menu,
    paths,
    probe,
    status,
    terraform,
    timeweb_dns,
    vdsina,
    watchdog,
    world_release,
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


def _bridge_transport(
    tf_dir: Path,
    chain_mode: str | None = None,
    bridge_port: int | None = None,
    *,
    manual_bridge: bool = False,
) -> tuple[str, int]:
    """Resolve the two hops independently, before writing inventory or running Ansible.

    Explicit chain mode wins over the environment, then the provider default.
    Timeweb's legacy defaults are SS and port 8443; other/manual bridges use XHTTP/443.
    Changing the chain alone never changes the provider's client-facing port.
    RAW needs its own WORLD inbound and is deliberately not accepted yet.
    """
    selected = chain_mode if chain_mode is not None else os.environ.get("PROXY_PIPELINE_CHAIN_MODE")
    if selected is not None:
        selected = selected.strip().lower()
        if selected not in {"ss", "xhttp"}:
            raise ValueError("chain mode must be 'ss' or 'xhttp' (RAW is not implemented yet)")
    if bridge_port is not None:
        if isinstance(bridge_port, bool) or str(bridge_port) != str(int(bridge_port)):
            raise ValueError("bridge port must be an integer between 1 and 65535")
        bridge_port = int(bridge_port)
        if not 1 <= bridge_port <= 65535:
            raise ValueError("bridge port must be an integer between 1 and 65535")

    default_mode, default_port = "xhttp", 443
    if not manual_bridge and (selected is None or bridge_port is None):
        try:
            state = terraform.terraform_state_list(tf_dir)
        except Exception:  # noqa: BLE001 — preserve the existing provider fallback
            state = []
        if any(a.endswith(".twc_server.bridge") for a in state):
            default_mode, default_port = "ss", 8443
    return selected or default_mode, bridge_port if bridge_port is not None else default_port


def _bridge_chain_mode(tf_dir: Path, override: str | None = None) -> str:
    """Compatibility helper for callers interested only in the chain."""
    return _bridge_transport(tf_dir, override)[0]


def _bridge_release(cfg: dict) -> dict[str, str]:
    """Require an explicit artifact pin before any play can mutate WORLD or RU."""
    release = cfg.get("xray", {}).get("bridge_release", {})
    if not isinstance(release, dict):
        raise ValueError("xray.bridge_release must contain version and archive_sha256")
    version = str(release.get("version", "")).strip()
    checksum = str(release.get("archive_sha256", "")).strip().lower()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("set xray.bridge_release.version to the approved Xray version (x.y.z)")
    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("set xray.bridge_release.archive_sha256 for the RU architecture")
    archive = str(release.get("archive_path", "")).strip()
    if archive:
        path = Path(archive).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError("xray.bridge_release.archive_path must be an existing absolute file")
        archive = str(path)
    return {
        "bridge_xray_version": version,
        "bridge_xray_archive_sha256": checksum,
        "bridge_xray_archive_path": archive,
    }


def _certificate_problem(cert: Path, key: Path, domain: str) -> str | None:
    """Why this certificate cannot serve the bridge name; None when it can."""
    def openssl(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["openssl", *args], capture_output=True, text=True, timeout=30)

    for path, label in ((cert, "certificate"), (key, "private_key")):
        if not path.is_absolute() or not path.is_file():
            return f"bridge_entry.{label} must be an existing absolute file"
    if openssl("x509", "-in", str(cert), "-noout", "-checkend", str(7 * 86400)).returncode != 0:
        return f"{cert.name} is unreadable or expires within 7 days: renew it first"
    host = openssl("x509", "-in", str(cert), "-noout", "-checkhost", domain)
    if "does match certificate" not in host.stdout:
        return f"{cert.name} is not valid for {domain}"
    public = openssl("x509", "-in", str(cert), "-noout", "-pubkey")
    derived = openssl("pkey", "-in", str(key), "-pubout")
    if public.returncode != 0 or derived.returncode != 0 or public.stdout != derived.stdout:
        return f"{key.name} is not the private key of {cert.name}"
    return None


def _bridge_entry(cfg: dict, bridge_port: int | None) -> dict:
    """HTTPS/XHTTP entry of the bridge: empty without bridge_entry.domain.

    With a domain nginx owns port 443 on RU, so the RAW/REALITY inbound moves to
    legacy_reality_port. The certificate is checked here, before inventory or Ansible: name,
    remaining lifetime and that the key belongs to it.
    """
    entry = cfg.get("bridge_entry") or {}
    if not isinstance(entry, dict):
        raise ValueError("bridge_entry must be a mapping")
    domain = str(entry.get("domain") or "").strip()
    if not domain:
        return {}
    if not re.fullmatch(r"([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain):
        raise ValueError("bridge_entry.domain must be a lower-case host name such as lk.example.ru")
    mode = str(entry.get("xhttp_mode") or "auto").strip()
    if mode not in {"auto", "stream-up", "packet-up"}:
        raise ValueError("bridge_entry.xhttp_mode must be auto, stream-up or packet-up")
    legacy = bridge_port if bridge_port is not None else entry.get("legacy_reality_port", 8443)
    if isinstance(legacy, bool) or not str(legacy).isdecimal() or not 1 <= int(legacy) <= 65535:
        raise ValueError("bridge_entry.legacy_reality_port must be a port number")
    if int(legacy) in (80, 443):
        raise ValueError(
            "with bridge_entry.domain nginx owns ports 80/443: move the RAW/REALITY inbound with"
            " bridge_entry.legacy_reality_port (default 8443) instead of --bridge-port 443"
        )
    # Explicit files win; otherwise what `pipelinectl cert issue` published for the zone.
    cert_path, key_path = str(entry.get("certificate") or ""), str(entry.get("private_key") or "")
    if not (cert_path and key_path):
        issued_cert, issued_key = certs.published(paths.state_dir(cfg), certs.zone_of(cfg))
        cert_path, key_path = cert_path or str(issued_cert), key_path or str(issued_key)
    cert, key = Path(cert_path).expanduser(), Path(key_path).expanduser()
    problem = _certificate_problem(cert, key, domain)
    if problem:
        raise ValueError(problem)
    site = str(entry.get("site_dir") or "").strip()
    if site and not Path(site).expanduser().is_dir():
        raise ValueError("bridge_entry.site_dir must be an existing directory")
    # Other names of the zone that show only the page (the bare domain, www): same certificate.
    site_names = entry.get("site_names") or []
    if not isinstance(site_names, list):
        raise ValueError("bridge_entry.site_names must be a list of host names")
    site_names = [str(name).strip() for name in site_names]
    for name in site_names:
        if name == domain or not re.fullmatch(r"([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", name):
            raise ValueError(f"bridge_entry.site_names: {name!r} must be another lower-case host name")
        problem = _certificate_problem(cert, key, name)
        if problem:
            raise ValueError(problem)
    return {
        "bridge_xhttp_enabled": True,
        "bridge_xhttp_mode": mode,
        "bridge_https_domain": domain,
        "bridge_https_cert": str(cert),
        "bridge_https_key": str(key),
        "bridge_https_site_dir": str(Path(site).expanduser()) if site else "",
        "bridge_https_site_names": site_names,
        "bridge_reality_port": int(legacy),
    }


def _firewall_vars(cfg: dict, state_dir: Path, bridge_ip: str | None, direct_only: bool) -> dict:
    """WORLD firewall inputs, checked before inventory or Ansible.

    The direct policy has no default: closing direct would cut off working clients, opening
    it would undo a deliberate hardening. A missing bridge is accepted only as the explicit
    direct-only mode, so a forgotten --bridge-ip cannot lock a manual RU out of the chain port.
    """
    fw = cfg.get("firewall") or {}
    if not isinstance(fw, dict):
        raise ValueError("firewall must be a mapping with direct_policy")
    policy = str(fw.get("direct_policy") or "").strip().lower()
    if policy not in {"public", "allowlist", "closed"}:
        raise ValueError(
            "set firewall.direct_policy to public, allowlist or closed (there is no default)"
        )

    def cidrs(key: str) -> list[str]:
        values = fw.get(key) or []
        if not isinstance(values, list):
            raise ValueError(f"firewall.{key} must be a list of IP addresses or CIDRs")
        for value in values:
            try:
                net = ipaddress.ip_network(str(value).strip(), strict=False)
            except ValueError:
                raise ValueError(f"firewall.{key}: {value!r} is not an IP address or CIDR") from None
            if net.prefixlen == 0:
                raise ValueError(f"firewall.{key}: {value!r} covers the whole internet")
        return [str(value).strip() for value in values]

    direct_cidrs, admin_cidrs = cidrs("direct_cidrs"), cidrs("admin_cidrs")
    ssh_world = fw.get("ssh_world", False)
    if not isinstance(ssh_world, bool):
        raise ValueError("firewall.ssh_world must be true or false")
    if policy == "allowlist" and not direct_cidrs:
        raise ValueError("firewall.direct_policy allowlist needs firewall.direct_cidrs")
    if direct_only:
        if bridge_ip:
            raise ValueError(f"direct-only conflicts with the bridge {bridge_ip}")
        if policy == "closed":
            raise ValueError("direct-only with firewall.direct_policy closed leaves WORLD without clients")
        recorded = state_dir / "bridge_runtime.json"
        if recorded.exists():
            raise ValueError(
                f"direct-only, but {recorded.name} still records a bridge: pass --bridge-ip,"
                " or remove that state after decommissioning the bridge"
            )
    elif not bridge_ip:
        raise ValueError(
            "no bridge IP (terraform output bridge_ip is empty): pass --bridge-ip for a manual RU,"
            " or select direct-only explicitly (--direct-only / firewall.direct_only)"
        )
    return {
        "exit_fw_bridge_ip": bridge_ip or "",
        "exit_fw_direct_only": direct_only,
        "exit_fw_direct_policy": policy,
        "exit_fw_direct_cidrs": direct_cidrs,
        "exit_fw_extra_admin_cidrs": admin_cidrs,
        "exit_fw_ssh_world": ssh_world,
    }


def _do_configure(
    bridge_only: bool = False,
    bridge_ip_override: str | None = None,
    bridge_user_override: str | None = None,
    bridge_sni: str | None = None,
    bridge_dest: str | None = None,
    exit_firewall_only: bool = False,
    chain_mode: str | None = None,
    bridge_port: int | None = None,
    bootstrap_panel: bool = False,
    direct_only: bool = False,
    exit_firewall_rollback: str | None = None,
    preflight: bool = False,
    separate_chain_identity: bool = False,
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
    rollback = exit_firewall_rollback is not None
    if bootstrap_panel and (bridge_only or exit_firewall_only or rollback):
        raise typer.BadParameter("--bootstrap-panel requires a full WORLD configure")
    if separate_chain_identity and (bridge_only or exit_firewall_only or rollback):
        raise typer.BadParameter("--separate-chain-identity requires a full WORLD configure")
    if rollback and (bridge_only or exit_firewall_only or preflight):
        raise typer.BadParameter("--exit-firewall-rollback runs on its own")
    if rollback and not re.fullmatch(r"latest|[0-9]{8}T[0-9]{6}Z(-[0-9]+)?", exit_firewall_rollback):
        raise typer.BadParameter("--exit-firewall-rollback takes 'latest' or a snapshot name")
    fw_cfg = cfg.get("firewall")
    direct_only = direct_only or (isinstance(fw_cfg, dict) and fw_cfg.get("direct_only") is True)
    manual = config.manual_bridge(cfg)
    if manual and not (bridge_ip_override or "").strip():
        # A RU bridge outside Terraform is declared in config; --bridge-ip still wins.
        bridge_ip_override = manual["ip"]
        bridge_user_override = bridge_user_override or manual["ssh_user"]
    if direct_only and bridge_only:
        raise typer.BadParameter("--bridge-only cannot be combined with direct-only")
    journal = st / ".world-transaction.json"
    if bridge_only and (journal.exists() or journal.is_symlink()):
        typer.secho("[configure] recover the unfinished WORLD transaction before bridge-only", fg=typer.colors.RED)
        raise typer.Exit(1)
    try:
        chain_mode, bridge_reality_port = _bridge_transport(
            tf_dir,
            chain_mode,
            bridge_port if bridge_port is not None else cfg.get("xray", {}).get("bridge_reality_port"),
            manual_bridge=bool((bridge_ip_override or "").strip()),
        )
    except (TypeError, ValueError) as e:
        typer.secho(f"[configure] {e}", fg=typer.colors.RED)
        raise typer.Exit(1) from e
    outs = terraform.terraform_output_json(tf_dir)
    exit_ip = outs.get("exit_ip") or ""
    if not exit_ip and not preflight:
        typer.secho(
            "terraform output exit_ip is empty — apply Terraform first.", fg=typer.colors.RED
        )
        raise typer.Exit(1)
    bridge_ip = (bridge_ip_override or "").strip() or (outs.get("bridge_ip") or "").strip() or None
    bridge_user_tf = (outs.get("bridge_ssh_user") or "").strip()
    bridge_user = (
        (bridge_user_override or "").strip() or bridge_user_tf or config.bridge_ssh_user(cfg)
    )
    if bridge_only and not bridge_ip:
        typer.secho(
            "--bridge-only set but terraform output bridge_ip is empty.", fg=typer.colors.RED
        )
        raise typer.Exit(1)
    try:
        firewall_only = exit_firewall_only or rollback
        release_vars = _bridge_release(cfg) if bridge_ip and not firewall_only else {}
        entry_vars = _bridge_entry(cfg, bridge_port) if bridge_ip and not firewall_only else {}
        if entry_vars:
            bridge_reality_port = entry_vars["bridge_reality_port"]
        fw_vars = {} if bridge_only or rollback else _firewall_vars(cfg, st, bridge_ip, direct_only)
        world_vars: dict[str, object] = {}
        if not (bridge_only or firewall_only):
            if not config.panel_pass(cfg):
                raise ValueError(
                    "panel_pass missing: set PROXY_PIPELINE_PANEL_PASS or config/pipeline.yaml"
                )
            # The role compares this pin with the node before bootstrap or any API change.
            world_vars = {f"world_{k}": v for k, v in world_release.pin_from_config(cfg).items()}
        if separate_chain_identity:
            # Only a target measured from THIS Exit may become the chain's REALITY identity.
            chain_target = probe.load_reality_target(st, purpose="chain")["selected"]
            if exit_ip and chain_target.get("exit_ip") != exit_ip:
                raise ValueError(
                    f"chain REALITY target was probed from {chain_target.get('exit_ip')}, not from"
                    f" {exit_ip}: run 'pipelinectl probe-targets --purpose chain' first"
                )
            world_vars.update(
                world_separate_chain=True,
                chain_reality_sni=chain_target["sni"],
                chain_reality_dest=chain_target["dest"],
            )
    except (TypeError, ValueError, FileNotFoundError) as e:
        typer.secho(f"[configure] {e}", fg=typer.colors.RED)
        raise typer.Exit(1) from e
    if preflight:
        typer.echo("[configure] preflight OK: inputs are valid; inventory and nodes NOT touched")
        return
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

    if rollback:
        playbook = adir / "playbooks" / "exit_firewall.yml"
        typer.echo(
            f"[configure] exit-firewall-rollback: restoring snapshot '{exit_firewall_rollback}'"
            " (inbounds NOT touched)"
        )
        ansible_runner.run_playbook(
            adir,
            playbook,
            inv,
            {"exit_fw_action": "rollback", "exit_fw_snapshot": exit_firewall_rollback},
            limit="exit",
        )
        return

    if exit_firewall_only:
        playbook = adir / "playbooks" / "exit_firewall.yml"
        xray = cfg.get("xray", {})
        typer.echo(
            f"[configure] exit-firewall-only: {playbook.name} (inbounds NOT touched)"
            f"  bridge={bridge_ip or 'none (direct-only)'}  direct={fw_vars['exit_fw_direct_policy']}"
        )
        ansible_runner.run_playbook(
            adir,
            playbook,
            inv,
            {
                **fw_vars,
                "reality_port": int(xray.get("reality_port", 443)),
                "ss_port": int(xray.get("ss_port", 8388)),
                "chain_port": int(xray.get("chain_port", 8443)),
            },
            limit="exit",
        )
        return

    if bridge_only:
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
                **release_vars,
                **entry_vars,
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
        "panel_bootstrap": bootstrap_panel or panel.get("bootstrap", False),
        "reality_sni": reality_sni,
        "reality_dest": reality_dest,
        "reality_port": int(xray.get("reality_port", 443)),
        "ss_port": int(xray.get("ss_port", 8388)),
        "ss_method": str(xray.get("ss_method", "2022-blake3-aes-128-gcm")),
        "chain_port": int(xray.get("chain_port", 8443)),
        "bridge_chain_mode": chain_mode,
        "bridge_reality_port": bridge_reality_port,
        **fw_vars,
        **world_vars,
        **release_vars,
        **entry_vars,
        **overrides,
    }

    playbook = (
        adir / "playbooks" / "site.yml" if bridge_ip else adir / "playbooks" / "exit_only.yml"
    )
    if separate_chain_identity:
        typer.secho(
            f"[configure] chain REALITY identity -> own key/shortId, sni={extra['chain_reality_sni']}:"
            " the chain is interrupted until the Bridge play of this run has finished",
            fg=typer.colors.YELLOW,
        )
    typer.echo(
        f"[configure] playbook -> {playbook.name}  firewall: direct={fw_vars['exit_fw_direct_policy']}"
        f"  bridge={bridge_ip or 'none (direct-only)'}"
    )
    ansible_runner.run_playbook(adir, playbook, inv, extra)


@app.command()
def configure(
    bootstrap_panel: bool = typer.Option(
        False, "--bootstrap-panel",
        help="Initialize credentials/path on an EMPTY fresh SQLite panel; never use for routine configure",
    ),
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
    exit_firewall_rollback: str = typer.Option(
        None,
        "--exit-firewall-rollback",
        metavar="SNAPSHOT",
        help="Only restore a saved Exit firewall: 'latest' (before the last change) or a snapshot name",
    ),
    direct_only: bool = typer.Option(
        False,
        "--direct-only",
        help="WORLD without a bridge: no bridge rules in the firewall (same as firewall.direct_only)",
    ),
    preflight: bool = typer.Option(
        False,
        "--preflight",
        help="Validate configuration inputs and stop before inventory or Ansible",
    ),
    separate_chain_identity: bool = typer.Option(
        False,
        "--separate-chain-identity",
        help="Give the RU -> WORLD chain its own REALITY key/shortId and the target from"
        " 'probe-targets --purpose chain'; direct client links are not affected",
    ),
    bridge_ip: str = typer.Option(
        None, "--bridge-ip", help="Override bridge IP (use when a -target apply left outputs stale)"
    ),
    bridge_user: str = typer.Option(
        None, "--bridge-user", help="Override bridge SSH user (Timeweb=root, Yandex=ubuntu)"
    ),
    chain_mode: str = typer.Option(
        None, "--chain-mode", help="RU -> WORLD: xhttp or ss; overrides PROXY_PIPELINE_CHAIN_MODE"
    ),
    bridge_port: int = typer.Option(
        None, "--bridge-port", min=1, max=65535,
        help="Client-facing RU port, independent of chain mode; overrides xray.bridge_reality_port",
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
        chain_mode=chain_mode,
        bridge_port=bridge_port,
        bootstrap_panel=bootstrap_panel,
        direct_only=direct_only,
        exit_firewall_rollback=exit_firewall_rollback,
        preflight=preflight,
        separate_chain_identity=separate_chain_identity,
    )


@app.command("cert")
def cert_cmd(
    action: str = typer.Argument("status", help="status | issue | renew"),
    zone: str = typer.Option(None, "--zone", help="DNS zone; default from bridge_entry"),
    staging: bool = typer.Option(
        False, "--staging", help="Let's Encrypt test CA: rehearse without touching the real certificate"
    ),
    force: bool = typer.Option(False, "--force", help="Renew even when the certificate is not due"),
    deploy: bool = typer.Option(
        False, "--deploy", help="renew: when the certificate changed, deliver it with configure --bridge-only"
    ),
) -> None:
    """Wildcard certificate for the bridge zone: DNS-01 on the controller with the pinned lego.

    issue/renew run the same client; it obtains a certificate when there is none and renews only
    when one is due. The result is published under state/certs/<zone>/ and delivered to RU by
    `configure` (nginx is reloaded, Xray is not restarted). Nothing is done on RU by this command
    unless --deploy is given.
    """
    if action not in {"status", "issue", "renew"}:
        raise typer.BadParameter("must be status, issue or renew", param_hint="ACTION")
    cfg = _cfg()
    st = paths.state_dir(cfg)
    try:
        zone = (zone or certs.zone_of(cfg)).strip().lower()
        if action == "status":
            for label, flag in (("production", False), ("staging", True)):
                info = certs.describe(certs.published(st, zone, flag)[0])
                if not info["present"]:
                    typer.echo(f"[cert] {label}: none for {zone}")
                elif not info["readable"]:
                    typer.secho(f"[cert] {label}: unreadable certificate file", fg=typer.colors.RED)
                else:
                    typer.echo(
                        f"[cert] {label}: {', '.join(info['names'])}  until {info['not_after']}"
                        f"  (at least {info['valid_at_least_days']} days)  issuer: {info['issuer']}"
                    )
            return
        result = certs.issue(cfg, st, zone, staging=staging, force=force)
    except (ValueError, OSError) as e:
        typer.secho(f"[cert] {e}", fg=typer.colors.RED)
        raise typer.Exit(1) from e
    typer.echo(
        f"[cert] {'staging ' if staging else ''}{', '.join(result['names'])}:"
        f" {'new certificate published' if result['changed'] else 'unchanged (not due for renewal)'}"
        f" -> {result['certificate']}"
    )
    if deploy and action == "renew" and result["changed"] and not staging:
        typer.echo("[cert] delivering the renewed certificate: configure --bridge-only")
        _do_configure(bridge_only=True)


@app.command("dns")
def dns_cmd(
    action: str = typer.Argument("show", help="show | ensure"),
    host: str = typer.Option(None, "--host", help="Bridge host name; default bridge_entry.domain"),
    ip: str = typer.Option(None, "--ip", help="Bridge IPv4; default bridge_manual.ip, else bridge_ip from state"),
    zone: str = typer.Option(None, "--zone", help="DNS zone; default from bridge_entry"),
    replace: bool = typer.Option(False, "--replace", help="ensure: move an existing A record to the new address"),
    site: list[str] = typer.Option(
        None, "--site", help="Also publish this name (bare domain, www); default bridge_entry.site_names"
    ),
) -> None:
    """DNS zone of the bridge at Timeweb Cloud: show its records, or publish the bridge's A record.

    `ensure` creates one A record for the bridge host and refuses a wildcard, CNAME or AAAA
    record that would send some clients elsewhere. An existing different address is changed
    only with --replace.
    """
    if action not in {"show", "ensure"}:
        raise typer.BadParameter("must be show or ensure", param_hint="ACTION")
    cfg = _cfg()
    try:
        host = (host or str((cfg.get("bridge_entry") or {}).get("domain") or "")).strip().lower()
        site_names = [str(n) for n in ((cfg.get("bridge_entry") or {}).get("site_names") or [])]
        zone = (zone or (certs.zone_of(cfg) if not host else host.partition(".")[2])).strip().lower()
        auth = timeweb_dns.token(cfg)
        if not auth:
            raise ValueError("no DNS API token: set TIMEWEBCLOUD_AUTH_TOKEN (or TWC_TOKEN) in the environment or .env")
        request = timeweb_dns.api(auth)
        if action == "ensure":
            if not host:
                raise ValueError("pass --host or set bridge_entry.domain")
            # The declared manual bridge first: on a first install there is no state yet.
            address = (
                ip
                or config.manual_bridge(cfg).get("ip")
                or str(health.load_runtime(paths.state_dir(cfg)).get("bridge_ip") or "")
            ).strip()
            if not address:
                raise ValueError("pass --ip: no bridge_manual.ip in config and no bridge_ip in state")
            # The bridge host, then the names that only show the page (bare domain, www).
            names = [host] + [str(n).strip().lower() for n in (site if site is not None else site_names)]
            for name in dict.fromkeys(names):
                result = timeweb_dns.ensure_a(request, zone, name, address, replace=replace)
                typer.echo(
                    f"[dns] {name} -> {address}: " + ("created" if result["changed"] else "already published")
                    + (f" (replaced {', '.join(result['replaced'])})" if result.get("replaced") else "")
                )
        for name in dict.fromkeys(filter(None, (zone, host, *site_names, *(site or [])))):
            for record in timeweb_dns.records(request, name):
                typer.echo(f"[dns]   {record['type']:<5} {record['name']:<28} {record['value']}  ttl={record['ttl']}")
    except (ValueError, OSError) as e:
        typer.secho(f"[dns] {e}", fg=typer.colors.RED)
        raise typer.Exit(1) from e


@app.command("world-release")
def world_release_cmd(
    verify: bool = typer.Option(
        False, "--verify",
        help="Fetch the pinned archive (cached in state/artifacts) and check it against both pins",
    ),
    archive: Path = typer.Option(
        None, "--archive", exists=True, dir_okay=False,
        help="Local x-ui-linux-<arch>.tar.gz: with --verify use it instead of downloading;"
        " alone, print its checksums for filling in the pins",
    ),
    arch: str = typer.Option("amd64", "--arch", help="Exit architecture of the release archive"),
) -> None:
    """Check the pinned WORLD release: terraform.tfvars (install) against xray.world_release (verify).

    Without options nothing is downloaded. `rotate` runs --verify before an Exit is replaced, so
    a wrong checksum fails on the controller instead of on a half-installed new node.
    """
    cfg = _cfg()
    if archive and not verify:
        try:
            found = world_release.inspect_archive(archive)
        except ValueError as e:
            typer.secho(f"[world-release] {e}", fg=typer.colors.RED)
            raise typer.Exit(1) from e
        typer.echo(f"[world-release] {archive.name}: checksums only, nothing was compared")
        typer.echo(f"  terraform.tfvars    xui_archive_sha256 = \"{found['archive_sha256']}\"")
        typer.echo(f"  xray.world_release  xray_sha256: {found['xray_sha256']}  ({found['xray_member']})")
        return
    try:
        pin, tf_pin = world_release.pin_from_config(cfg), world_release.terraform_pin(cfg)
        world_release.require_same_version(pin, tf_pin)
        typer.echo(
            f"[world-release] 3X-UI {pin['xui_version']}  Xray {pin['xray_version']}"
            "  (terraform.tfvars and xray.world_release agree)"
        )
        if verify:
            found = world_release.verify(cfg, paths.state_dir(cfg) / "artifacts", arch, archive)
            typer.echo(f"[world-release] verified {found['archive']}: archive and bundled Xray match")
    except (ValueError, OSError) as e:
        typer.secho(f"[world-release] {e}", fg=typer.colors.RED)
        raise typer.Exit(1) from e


@app.command("probe-targets")
def probe_targets_cmd(
    targets_file: Path = typer.Option(
        None,
        "--targets",
        help="Candidate YAML; defaults to reality_targets[.chain].yaml for the selected purpose",
        exists=True,
    ),
    fmt: str = typer.Option("text", "--format", help="text or json"),
    purpose: str = typer.Option(
        "direct", "--purpose", help="direct or chain; chain writes separate trial state"
    ),
) -> None:
    """Probe TLS/ALPN from WORLD; this does not authenticate a RU -> WORLD VLESS session."""
    if purpose not in {"direct", "chain"}:
        raise typer.BadParameter("must be direct or chain", param_hint="--purpose")
    cfg = _cfg()
    root = paths.repo_root()
    st = paths.state_dir(cfg)

    default_name = "reality_targets.chain.yaml" if purpose == "chain" else "reality_targets.yaml"
    tf_path = targets_file or root / "config" / default_name
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
        report["purpose"] = purpose
        out_path = probe.write_reality_target(st, report, purpose=purpose)
        typer.secho(f"[probe-targets] wrote failed report → {out_path}", fg=typer.colors.YELLOW)
        raise typer.Exit(1) from e

    report["purpose"] = purpose
    out_path = probe.write_reality_target(st, report, purpose=purpose)
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


@app.command("status")
def status_cmd(
    outside: bool = typer.Option(
        True, "--outside/--no-outside",
        help="Also ask ~20 Russian networks (Globalping) whether they reach the bridge address",
    ),
) -> None:
    """Name the broken part of the chain (Exit, bridge, DNS, certificate) and the command to fix it.

    Runs the checks of `validate --deep`; exit code = number of broken parts. Changes nothing.
    """
    cfg = _cfg()
    st = paths.state_dir(cfg)
    data = health.load_runtime(st)
    bridge_key = config.ssh_key_for_bridge(cfg) if data.get("bridge_ip") else None
    rpt = health.run_health(st, config.ssh_key_for_exit(cfg), bridge_key, config.bridge_ssh_user(cfg))
    if data.get("exit_ip"):
        deep_rpt = deep_health.run_deep_health(
            data,
            config.ssh_key_for_exit(cfg),
            config.panel_pass(cfg),
            config.exit_ssh_user(cfg),
            bridge_ip=str(data.get("bridge_ip") or ""),
            bridge_user=str(data.get("bridge_ssh_user") or config.bridge_ssh_user(cfg)),
            bridge_key=bridge_key,
        )
        rpt.results.extend(deep_rpt.results)
    reach, note = None, ""
    bridge_ip, domain = str(data.get("bridge_ip") or ""), str(data.get("bridge_domain") or "")
    if outside and bridge_ip and domain:
        try:
            reach = status.outside_reach(bridge_ip, domain)
            note = f"outside: {reach[0]} of {reach[1]} Russian networks reached {bridge_ip}:443"
        except (OSError, ValueError, KeyError) as e:
            note = f"outside: not measured ({e})"
    manual = cfg.get("bridge_manual") if isinstance(cfg.get("bridge_manual"), dict) else {}
    providers = {"bridge": str(manual.get("provider") or "manual RU server")} if manual else {}
    parts = status.diagnose(
        rpt.results, reach, providers,
        declared_bridge=config.manual_bridge(cfg).get("ip", ""), recorded_bridge=bridge_ip,
        bridge_automatic=_bridge_kind(cfg) in ("vdsina", "yandex"),
    )
    typer.echo(status.render(parts, note))
    raise typer.Exit(sum(not p.ok for p in parts))


@app.command("menu")
def menu_cmd() -> None:
    """Interactive menu: status, the chain, checks, links, node replacement, settings."""
    menu.run_menu()


@app.command("backup")
def backup_cmd(
    node: str = typer.Option("all", "--node", help="all | exit | bridge | local (controller files only)"),
    verify: Path = typer.Option(
        None, "--verify", exists=True, file_okay=False,
        help="Only re-check an existing backup directory against its manifest",
    ),
) -> None:
    """Save controller state and node configuration before a change, then verify the copy.

    Writes backups/<UTC time>/ (0700): local.tar with state, tfstate, config and client links;
    exit.tar with a consistent copy of the 3X-UI database, the generated Xray config, sshd and
    ufw files; bridge.tar with the RU Xray config, unit, sshd, nginx and ufw files. Nodes are
    only read. The archives contain credentials: keep them on the controller. Exit code 2 when
    a source could not be saved or verification fails.
    """
    if verify is not None:
        problems = backup.verify(verify)
    else:
        if node not in {"all", "exit", "bridge", "local"}:
            raise typer.BadParameter("must be all, exit, bridge or local", param_hint="--node")
        cfg = _cfg()
        st = paths.state_dir(cfg)
        data = health.load_runtime(st)
        nodes = []
        if node in ("all", "exit") and data.get("exit_ip"):
            nodes.append((str(data["exit_ip"]), config.exit_ssh_user(cfg), config.ssh_key_for_exit(cfg), "exit"))
        if node in ("all", "bridge") and data.get("bridge_ip"):
            bridge_user = str(data.get("bridge_ssh_user") or config.bridge_ssh_user(cfg))
            nodes.append((str(data["bridge_ip"]), bridge_user, config.ssh_key_for_bridge(cfg), "bridge"))
        try:
            verify, manifest = backup.create(paths.repo_root(), st, nodes)
        except FileExistsError as e:
            typer.secho(f"[backup] a backup from this very second exists; run again ({e})", fg=typer.colors.RED)
            raise typer.Exit(2) from e
        typer.echo(f"[backup] {verify}  git={manifest['git']['sha'][:12]}"
                   f"{' (dirty tree)' if manifest['git']['dirty'] else ''}")
        for name, entry in manifest["archives"].items():
            typer.echo(f"[backup]   {name}.tar  {entry['size']} bytes  {len(entry['members'])} files")
        for name, absent in manifest["missing"].items():
            if absent:
                typer.echo(f"[backup]   {name}: not present on the node: {', '.join(absent)}")
        problems = backup.verify(verify)
    for problem in problems:
        typer.secho(f"[backup] PROBLEM {problem}", fg=typer.colors.RED)
    if problems:
        raise typer.Exit(2)
    typer.echo(f"[backup] verified {verify}: archives match the manifest, databases and JSON are readable")


@app.command("inventory")
def inventory_cmd(
    node: str = typer.Option("all", "--node", help="all | exit | bridge"),
    fmt: str = typer.Option("text", "--format", help="text or json"),
) -> None:
    """Read-only facts from WORLD and RU over SSH: versions for the pins, listeners, sshd, firewall.

    Nothing is changed on the nodes and no secrets are read. Use it to choose
    xray.world_release / xray.bridge_release and as the baseline before SSH or firewall work.
    Exit code 2 when a node could not be read.
    """
    if node not in {"all", "exit", "bridge"}:
        raise typer.BadParameter("must be all, exit or bridge", param_hint="--node")
    cfg = _cfg()
    data = health.load_runtime(paths.state_dir(cfg))
    targets = []
    if node in ("all", "exit") and data.get("exit_ip"):
        targets.append((str(data["exit_ip"]), config.exit_ssh_user(cfg), config.ssh_key_for_exit(cfg), "exit"))
    if node in ("all", "bridge") and data.get("bridge_ip"):
        bridge_user = str(data.get("bridge_ssh_user") or config.bridge_ssh_user(cfg))
        targets.append((str(data["bridge_ip"]), bridge_user, config.ssh_key_for_bridge(cfg), "bridge"))
    if not targets:
        typer.secho("[inventory] no matching node in state/runtime.json / bridge_runtime.json", fg=typer.colors.RED)
        raise typer.Exit(1)
    nodes = [inventory.collect(*target) for target in targets]
    if fmt == "json":
        typer.echo(json.dumps(nodes, indent=2))
    else:
        typer.echo(inventory.render(nodes, cfg))
    raise typer.Exit(2 if any(n.get("error") for n in nodes) else 0)


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
    entry = str(data.get("bridge_domain") or "").strip()
    reserve = f"{bridge_ip}:{data.get('bridge_reality_port') or 443}"
    typer.echo(
        f"[doctor] Bridge:   {f'https://{entry}  (reserve RAW/REALITY {reserve})' if entry else reserve if bridge_ip else '—'}"
    )
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
    bridge_ip: str = typer.Option(
        None, "--bridge-ip", help="bridge: move the manual (non-Terraform) bridge to this new server"
    ),
    attempts: int = typer.Option(3, "--attempts", help="bridge on VDSina: servers to try before giving up"),
    delete_old: bool = typer.Option(
        False, "--delete-old", help="bridge on VDSina: delete the old server after a successful move"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="bridge on VDSina: show what would be ordered, change nothing"
    ),
) -> None:
    """Recreate Exit and/or Bridge and re-issue links, via scripts/rotate-chain.sh.

    `rotate bridge` picks the tool from how the bridge is run: a Yandex bridge in Terraform is
    recreated by Terraform; a VDSina bridge (bridge_manual.provider: VDSina) gets a new server
    through the VDSina API, checked from inside and outside before anything moves; any other
    manual bridge needs --bridge-ip of a server you created.

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
    if t == "bridge" and not bridge_ip:
        cfg = _cfg()
        kind = _bridge_kind(cfg)
        typer.echo(f"[rotate] bridge is run by: {kind}")
        if kind == "vdsina":
            raise typer.Exit(_rotate_vdsina_bridge(cfg, script, attempts, yes, delete_old, dry_run))
        if kind == "manual":
            typer.secho("this bridge's provider has no API here: create a new RU server with the controller's"
                        " SSH key, then run: pipelinectl rotate bridge --bridge-ip NEW_IP", fg=typer.colors.RED)
            raise typer.Exit(2)
        if kind == "none":
            typer.secho("no bridge: neither a Yandex bridge in Terraform nor bridge_manual in config",
                        fg=typer.colors.RED)
            raise typer.Exit(2)
        if dry_run:
            typer.echo("[rotate] dry-run: the Yandex bridge would be recreated by Terraform")
            raise typer.Exit(0)
    cmd = ["bash", str(script), t]
    if region:
        cmd += ["--region", region]
    if plan:
        cmd += ["--plan", plan]
    if yes:
        cmd += ["--yes"]
    if new_ip:
        cmd += ["--new-ip"]
    if bridge_ip:
        cmd += ["--bridge-ip", bridge_ip.strip()]
    raise typer.Exit(subprocess.call(cmd, cwd=str(paths.repo_root())))


def _bridge_kind(cfg: dict) -> str:
    """yandex (Terraform), vdsina (API), manual (any other server) or none."""
    try:
        listed = subprocess.run(
            ["terraform", f"-chdir={paths.terraform_dir(cfg)}", "state", "list"],
            capture_output=True, text=True, timeout=120,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        listed = ""
    if re.search(r"yandex_compute_instance\.bridge$", listed, re.M):
        return "yandex"
    manual = cfg.get("bridge_manual") if isinstance(cfg.get("bridge_manual"), dict) else {}
    if str(manual.get("provider") or "").strip().lower() == "vdsina" and config.manual_bridge(cfg):
        return "vdsina"
    return "manual" if config.manual_bridge(cfg) else "none"


def _rotate_vdsina_bridge(
    cfg: dict, script: Path, attempts: int, yes: bool, delete_old: bool, dry_run: bool
) -> int:
    """Order a VDSina server that passes every gate, then move the bridge onto it."""
    def fail(msg: str) -> int:
        typer.secho(f"[bridge] {msg}", fg=typer.colors.RED)
        return 1

    auth = vdsina.token()
    if not auth:
        return fail("set VDSINA_API_TOKEN in .env (VDSina panel -> account -> API)")
    request = vdsina.api(auth)
    st = paths.state_dir(cfg)
    manual = config.manual_bridge(cfg)
    data = health.load_runtime(st)
    domain = str(data.get("bridge_domain") or (cfg.get("bridge_entry") or {}).get("domain") or "")
    exit_ip, chain_port = str(data.get("exit_ip") or ""), int(data.get("chain_port") or 8443)
    key = config.ssh_key_for_bridge(cfg)
    pub = Path(str(key) + ".pub")
    try:
        if not (domain and exit_ip):
            raise ValueError("state has no Exit or no bridge domain: run configure first")
        cert, privkey = certs.published(st, certs.zone_of(cfg))
        if not (cert.is_file() and privkey.is_file() and pub.is_file()):
            raise ValueError(f"need {cert}, {privkey} and {pub}")
        old = vdsina.find_server(request, manual["ip"])
        spec = vdsina.spec_of(request, int(old["id"])) if old else {}
        overrides = (cfg.get("bridge_manual") or {}).get("vdsina") or {}
        spec.update({k: int(v) for k, v in overrides.items() if k in ("datacenter", "server-plan", "template")})
        if set(spec) != {"datacenter", "server-plan", "template"}:
            raise ValueError("old server not found by address: set bridge_manual.vdsina datacenter/server-plan/template")
        price = vdsina.plan_cost(request, spec["server-plan"]) or "price unknown"
    except (OSError, ValueError, KeyError, vdsina.ApiError) as e:
        return fail(str(e))
    typer.echo(f"[bridge] now: {manual['ip']}" + (f" (VDSina server {old['id']})" if old else ""))
    typer.echo(f"[bridge] new server: datacenter {spec['datacenter']}, plan {spec['server-plan']} ({price}),"
               f" template {spec['template']}; up to {attempts} attempt(s), rejected servers deleted at once")
    typer.echo("[bridge] old server after the move: " + ("deleted" if delete_old else "kept (delete it by hand)"))
    if dry_run:
        typer.echo("[bridge] dry-run: nothing ordered")
        return 0
    if not yes and not typer.confirm(f"Order VDSina servers at {price} each?", default=False):
        return fail("aborted")
    try:
        key_id = vdsina.ensure_ssh_key(request, pub.read_text())
        user = manual["ssh_user"]
        cand = bridge_rotate.find_good_server(
            request, spec, key_id,
            check=lambda ip: bridge_rotate.check_candidate(
                ip, user, key, exit_ip=exit_ip, chain_port=chain_port, cert=cert, privkey=privkey, host=domain),
            ready=lambda ip: bridge_rotate.wait_ssh(ip, user, key),
            attempts=attempts, echo=typer.echo, name=f"pp-bridge-{datetime.now(timezone.utc):%m%d%H%M}",
        )
    except (OSError, vdsina.ApiError) as e:
        return fail(str(e))
    if cand is None:
        return fail(f"no server passed the checks in {attempts} attempt(s); the bridge was not touched")
    typer.echo(f"[bridge] {cand.ip} passed every check — moving the bridge")
    rc = subprocess.call(["bash", str(script), "bridge", "--bridge-ip", cand.ip], cwd=str(paths.repo_root()))
    if rc != 0:
        typer.secho(f"[bridge] the move failed; new server {cand.server_id} ({cand.ip}) is kept for inspection",
                    fg=typer.colors.RED)
        return rc
    if old and delete_old:
        vdsina.delete_server(request, int(old["id"]))
        typer.echo(f"[bridge] old server {old['id']} ({manual['ip']}) deleted")
    elif old:
        typer.echo(f"[bridge] old server {old['id']} ({manual['ip']}) still runs and is billed: delete it in the"
                   " VDSina panel once clients work")
    return 0


def main() -> None:
    app()


if __name__ == "__main__":
    main()
