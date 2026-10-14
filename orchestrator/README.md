# pipelinectl

```bash
cd orchestrator
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pipelinectl --help
```

Requires on PATH: `terraform`, `ansible-playbook`, `jq`, `curl`, `ssh`.

Set `PROXY_PIPELINE_ROOT` to the repository root if running from elsewhere.

## Bridge transport selection

`configure --chain-mode xhttp|ss` selects only the RU → WORLD transport. Precedence:
explicit flag, then `PROXY_PIPELINE_CHAIN_MODE`, then the provider default. Unknown or
empty values fail before inventory writes or Ansible. RAW is not implemented yet.

`--bridge-port` controls the existing client-facing RAW/REALITY listener independently.
It overrides `xray.bridge_reality_port` in `config/pipeline.yaml`. Without either setting,
the legacy provider defaults remain: Timeweb uses SS/8443; Yandex and a manual
`--bridge-ip` use XHTTP/443. Selecting `--chain-mode ss` on a manual RU keeps port 443.
These options do not install the future Nginx/TLS/XHTTP frontend; deployment gates and
remaining work are tracked in [SPRINT-1](../docs/SPRINT-1.md).

## HTTPS entry of the bridge (own domain, nginx, XHTTP)

```yaml
bridge_entry:
  domain: lk.example.ru        # A record on the bridge address; no AAAA
  certificate: /abs/path/fullchain.pem
  private_key: /abs/path/privkey.pem
  legacy_reality_port: 8443    # where the RAW/REALITY inbound moves
  xhttp_mode: auto             # what the server accepts: auto | stream-up | packet-up
  site_names: [example.ru, www.example.ru]   # optional: the bare domain as a plain site
```

With a domain, `configure` (full or `--bridge-only`) turns RU into an ordinary HTTPS site:

- nginx listens on 443 with the delivered certificate (and on 80 only to redirect);
- one random secret path is relayed to Xray's `vless-xhttp-in` on `127.0.0.1:10443`: VLESS +
  XHTTP, empty `flow`, its own client UUID; everything else gets a static page;
- a request that does not name the bridge (an address scanner) gets a throwaway certificate and a
  dropped connection, so the address does not reveal the name;
- names in `site_names` (the bare domain, `www`) get the same page and nothing else: their
  server block has no path into the tunnel, so the domain is an ordinary site rather than a
  "personal account" of a site that does not exist;
- no access log is kept; no IPv6 listener exists because no AAAA record is published;
- the entry's traffic is routed like every client inbound: private destinations blocked,
  everything else only to `chain-to-exit`.

Before inventory or Ansible the CLI checks that the certificate is valid for the name, has more
than 7 days left and matches the key (`openssl`). On RU, nginx is installed without being started,
the certificate, page and config are written and `nginx -t` validates them while Xray still serves
the old config. Only then does the apply transaction move Xray off the public port, start nginx,
and verify from RU itself that the name validates, the page is served and an unknown name is not
shown the bridge certificate. A failure stops nginx first, then restores the previous Xray config
and controller state. A renewed certificate reloads nginx without restarting Xray; removing the
domain stops nginx before Xray returns to its port.

**Modes, measured on loopback with the real binaries (Xray 25.10.15, nginx 1.18):**

| Mode | nginx upstream | Result of 6 MB uploads |
|---|---|---|
| `stream-up` | `grpc_pass` (HTTP/2 to Xray), no body limit | intact in every run |
| `stream-up` | `proxy_pass` (HTTP/1.1) | no transfer; not supported |
| `packet-up` | `proxy_pass` (HTTP/1.1), 2 MB body limit | a 184-byte block duplicated in about 1 of 3 runs |
| `packet-up` | none (client straight to Xray, HTTP/1.1 or HTTP/2) | the same damage |

The damage is in packet-up itself on that old Xray, not in nginx or sniffing; on 26.3.27 (the
version on RU) four runs of each mode showed none. Downloads and slow responses passed
unbuffered in every mode.

`xhttp_mode` is what the **server accepts**, and the default is `auto`: every XHTTP mode,
relayed by `grpc_pass`. The client application chooses its own mode and may ignore the one in a
share link. A `stream-up`-only entry was live for half an hour on 2026-10-13 and neither
Shadowrocket nor v2rayN could connect: their requests got `400` from Xray. With `auto`, clients
on Xray cores 26.3.27 and 26.10.10 connect in `auto`, `packet-up`, `stream-up` and `stream-one`.
`packet-up` as a server setting (HTTP/1.1 relay) is only for a path through a buffering CDN.

Not automated yet: a renewal timer and the end-to-end client test (`pipelinectl test`). Turning
the entry off and on again issues a new UUID/path unless the live config still has them.

### Client link and monitoring of the entry

`pipelinectl links` adds `bridge_xhttp`, listed first as the main profile:

```
vless://<uuid>@lk.example.ru:443?encryption=none&security=tls&sni=lk.example.ru&alpn=h2&fp=chrome
      &type=xhttp&host=lk.example.ru&path=%2F<secret>&mode=auto#Bridge-XHTTP-lk.example.ru
```

It addresses the domain, not the IP, so an Exit rotation or a new bridge address (a DNS change)
leaves it valid. It has no `flow` and nothing of the RU -> WORLD chain. The RAW/REALITY and
SS2022 links remain, marked as the reserve, with the port the RAW inbound was moved to.

With the entry enabled the chain probe on RU records two more checks per round:

| Check | Meaning | Effect |
|---|---|---|
| `entry_https` | HTTPS to the bridge's own name on loopback: certificate chain, name, page | 3 failing rounds alert as DOWN |
| `entry_cert` | the certificate has more than `chain_probe_cert_days` (14) days left | shown in `chainlog` and `validate --deep`; no alert yet |

Their verdicts name the bridge ("HTTPS entry of the bridge down"), never the Exit, so the
watchdog alerts and does not rotate. `dest_tls` now describes only the reserve RAW/REALITY
profile. `validate --deep` adds the outside view from the controller, as two results because they
have different fixes: `entry_dns` (the name resolves to exactly the bridge address) and
`entry_https` (certificate and page over the public address). TCP checks follow the ports the
bridge really uses (`tcp_bridge_8443`, `tcp_bridge_443`).

### DNS record and certificate

```bash
pipelinectl dns show                       # zone apex and the bridge host, as Timeweb lists them
pipelinectl dns ensure [--replace]         # A records: bridge host and site_names -> bridge_ip
pipelinectl cert issue --staging           # rehearsal against Let's Encrypt's test CA
pipelinectl cert issue                     # wildcard certificate for the zone
pipelinectl cert renew [--deploy]          # renews only when due; --deploy runs configure --bridge-only
pipelinectl cert status
```

The zone is hosted at Timeweb Cloud. The token comes from `TIMEWEBCLOUD_AUTH_TOKEN`, else
`TWC_TOKEN` (environment or `.env`), and needs DNS access; it is passed to the ACME client in its
environment only. `dns ensure` creates one A record, is a no-op when it already matches, moves it
only with `--replace`, and refuses a host that also has an AAAA or CNAME record. Timeweb lists a
host's records under the host itself, not in the zone's list, and takes a record for the bare
domain only through its v2 endpoint (v1 answers `bad_subdomain_name`).

`cert` runs the pinned lego (`acme.lego_release`: version and SHA256 of the official archive,
downloaded once into `state/artifacts/`) on the controller with the DNS-01 challenge and asks
for `*.<zone>` and `<zone>` (so a site on the bare domain needs no reissue): nothing runs on RU,
no port is opened there, and the bridge host name does not appear in the public certificate logs. The certificate and key are published as
`state/certs/<zone>/fullchain.pem` and `privkey.pem` (0600); `bridge_entry` uses them unless
explicit files are configured. A failed run leaves the published files as they were. Staging
uses a separate directory and never touches them.

Timing at Timeweb, measured 2026-10-13: three of the four name servers serve a new record
within seconds, `ns3.timeweb.org` about twelve minutes later, and deleted records linger for a
similar time. The CA may query any of them, so the client waits for all authoritative servers
(up to `acme.propagation_timeout`, 1800 s by default) and an issuance takes 8-15 minutes. The
controller's own recursive resolver is not awaited, because it can hold a cached "no such name"
from the moment before the record appeared; resolvers for zone discovery are explicit
(`acme.resolvers`, default 1.1.1.1 and 8.8.8.8). A failed run writes the client's full log to
`state/acme/<staging|production>/last-failure.log`.

Verified against the real services: reading the zone, creating the A record and the no-op
repeat, a staging issuance (after two failed rehearsals that produced the settings above) and
the production issuance. Not yet exercised: renewal, `--replace`, and `cert renew --deploy`.

## Pinned Bridge installation and rollback

Before configuring a Bridge, fill `xray.bridge_release` in `config/pipeline.yaml`:

```yaml
xray:
  bridge_release:
    version: ""          # approved numeric version, e.g. x.y.z; no automatic latest
    archive_sha256: ""   # SHA256 of the official ZIP for this RU architecture
    archive_path: ""     # optional absolute controller path to an offline ZIP
```

Missing/invalid pins fail before inventory or Ansible, including full configure. Exit-only
and firewall-only plays do not require a Bridge pin. Select the version after inventorying
the existing RU binary; the installer refuses to replace a binary that differs from the
pinned artifact. Upgrades need a separate migration. Supported archives are
`Xray-linux-64.zip` (x86_64) and `Xray-linux-arm64-v8a.zip` (aarch64) from
[official Xray releases](https://github.com/XTLS/Xray-core/releases).

The controller downloads and checks the ZIP, or checks `archive_path` for offline delivery.
Ansible copies it over SSH; RU verifies SHA256, required files and executable version before
installing. RU does not need access to GitHub. Existing geodata and service units are preserved;
an unsupported effective `ExecStart` stops the role before config changes. A new unit uses
`nobody`, the configured binary and config path; preparation does not start/restart Xray.

Identity precedence remains live config → controller backup → fresh generation. A malformed
live config is an error. A valid live identity takes precedence over a stale backup; when
backup recovery is needed, an incomplete/corrupt backup is an error too. Only an absent
config (or the official installer's empty `{}` bootstrap) allows backup/fresh setup.
Unknown managed layouts and multiple client identities require explicit migration. Both
old `Public key` and newer `Password` x25519 output are accepted; scalar normalization does
not change the saved private-key encoding.

The RU transaction validates the candidate, activates it, checks the client TCP listener
and service activity, then publishes `bridge_runtime.json` and `bridge_identity.json`.
Ordinary task failures restore the previous config, service activity/enablement and both
controller files byte for byte; the command still exits with failure. An unchanged active
config does not restart Xray. This checks local startup, not end-to-end chain availability.

The rollback boundary excludes installation, resolver/sysctl, monitoring setup and WORLD.
It cannot recover automatically from lost SSH, a killed controller, or concurrent applies.
WORLD now has its own API transaction and a separate firewall transaction, both described
below. The HTTPS/XHTTP frontend remains sprint work; full configure has not been exercised on
working servers. Complete the sprint's SSH/backups gates before changing them.

## Pinned WORLD release

WORLD runs the Xray bundled with 3X-UI, so the pin has two consumers:

```hcl
# terraform/terraform.tfvars — what a NEW Exit installs (cloud-init, checksum-verified)
xui_version        = "vX.Y.Z"
xui_archive_sha256 = "<sha256 of x-ui-linux-amd64.tar.gz>"
```

```yaml
# config/pipeline.yaml — what configure must find on the node
xray:
  world_release:
    xui_version: ""    # x.y.z, same release as terraform.tfvars
    xray_version: ""   # bundled Xray, x.y.z
    xray_sha256: ""    # SHA256 of /usr/local/x-ui/bin/xray-linux-<arch>
```

Cloud-init no longer installs `latest`: it downloads the exact tag with `curl --fail`, checks the
archive SHA256 before unpacking, and writes the ready marker only afterwards. `user_data` is in
`ignore_changes`, so changing the pin never replaces the live Exit; it applies at the next
rotation. Both Terraform variables are required and have no default.

Full configure requires `xray.world_release` before inventory or Ansible. On the node the role
reads `x-ui -v`, `xray version` and the binary SHA256, and compares them with the pin before the
panel bootstrap and before any API change; a difference is reported with both sides and nothing
is modified. Upgrades and downgrades are a separate migration, never a side effect of configure.
Bridge-only, firewall-only and firewall rollback do not need this pin.

```bash
pipelinectl inventory                    # installed versions/checksums on WORLD and RU
pipelinectl world-release                # offline: both pins present and the same release
pipelinectl world-release --verify       # + fetch the archive, check it and its bundled Xray
pipelinectl world-release --archive x-ui-linux-amd64.tar.gz   # checksums of a local archive
```

`--verify` caches the archive in `state/artifacts/` and proves that the archive pinned for
Terraform contains the Xray pinned for configure. `rotate exit|all` runs it after
`configure --preflight` and before `terraform plan`, so a wrong checksum is found on the
controller rather than by a half-installed replacement after the old Exit is gone. The
checksum pins one architecture (amd64 by default, `--arch` otherwise).

## Inventory and backups before live changes

`pipelinectl inventory [--node all|exit|bridge] [--format json]` is read-only: OS, clock skew
against the controller, Xray path/version/SHA256, 3X-UI version, pin comparison
(`match`/`MISMATCH`/`unpinned`), service states, listening sockets with their processes,
effective `sshd -T` settings, authorized-key counts, fail2ban, ufw (including `IPV6=`) and
global IPv6 addresses. It prints no keys or configuration contents. Exit code 2 means a node
could not be read. Use it to choose the pins and as the baseline for SSH and firewall work.

`pipelinectl backup [--node all|exit|bridge|local]` writes `backups/<UTC time>/` (0700, files
0600, gitignored): `local.tar` (state except probe logs/artifacts, tfstate, `pipeline.yaml`,
`terraform.tfvars`, client links, `.env`), `exit.tar` (a consistent online copy of
`/etc/x-ui/x-ui.db`, the generated Xray config, the x-ui unit, sshd and ufw files) and
`bridge.tar` (RU Xray config and unit, sshd, nginx and ufw files), plus `manifest.json` with the
git SHA, a dirty-tree flag and per-file SHA256. It then verifies the result;
`pipelinectl backup --verify DIR` repeats that later: archive and member checksums, SQLite
`integrity_check`, JSON parsing. An unreachable node is recorded and the command exits 2. The
archives contain credentials and private keys and must stay on the controller.

Restoring is deliberately manual: stop the service, put the file back from the archive
(`tar -xOf exit.tar etc/x-ui/x-ui.db > /etc/x-ui/x-ui.db`), validate (`xray run -test`,
`sshd -t`), start, then run `pipelinectl validate --deep`. A restore has not been rehearsed on
the live nodes yet.

## WORLD identity and API recovery

`configure` preserves the existing managed direct UUID, REALITY keys/shortId/SNI/dest,
SS2022 password, chain UUID and XHTTP path. A new probe result seeds only a fresh identity.
Live panel state takes precedence over a valid controller runtime; runtime recovery is allowed
only for the same `exit_ip`. A different fresh WORLD gets a new identity. Malformed runtime,
partial/duplicate managed remarks, unsupported layouts or conflicting ports stop application.
A new WORLD always gets a separate REALITY key and shortId for the chain inbound. A WORLD
configured earlier keeps the shared parameters it serves until the explicit migration below.

### Separate chain REALITY identity

```bash
pipelinectl probe-targets --purpose chain        # measured from this Exit
pipelinectl configure --separate-chain-identity  # full configure: WORLD, then RU
```

The migration gives `Chain-Bridge-Entry` its own key pair and shortId (when they are still shared
with the direct inbound) and the SNI/dest selected by the chain probe; the probe must come from
the current Exit. Only that inbound is updated, by ID; the direct inbound, its client links and
the SS inbound are untouched. Repeating the command with a newly measured target replaces only
SNI/dest and keeps the key. Without the flag an existing chain identity is never changed, even if
a chain probe result is present. RU receives the chain public key, shortId and SNI from
`runtime.json` (`chain_public_key`, `chain_short_id`, `chain_reality_sni`); the bridge's own
client-facing keys are not involved, and the chain probe's `tls_exit` check uses the chain SNI.

The chain is interrupted from the WORLD update until the Bridge play of the same run finishes.
`site.yml` therefore checks that RU answers SSH before WORLD is touched. If the Bridge play
still fails, RU keeps its previous outbound while WORLD already serves the new identity: rerun
`pipelinectl configure` (WORLD is then unchanged, RU gets the new outbound). There is no single
RU+WORLD rollback; clients fall back to the direct profile meanwhile.

Existing inbounds are updated by ID; unchanged inbounds generate no API writes. The previous
del-before-add workaround is removed. Unmanaged inbounds remain intact. The helper fetches the
complete generated config (including unrelated inbounds/routing) and validates the candidate
with the installed WORLD Xray before any inbound mutation. After mutation it checks read-back,
the generated config, Xray status, TCP listeners and ownership of those listeners by the selected
Xray binary. Unexpected panel normalization (including tag/routing changes) causes rollback.
The installed binary version/SHA256 are recorded in runtime; the pin itself is enforced before
this transaction (see "Pinned WORLD release").

Panel sessions are in memory and all configure API requests use a dedicated SSH tunnel to
`127.0.0.1:<panel-port>`. The role requires the existing credentials/path; login failure never
resets a configured panel or restarts `x-ui`. For the first configure of a fresh SQLite panel:

```bash
pipelinectl configure --bootstrap-panel
```

Bootstrap verifies `/etc/x-ui/x-ui.db` has no inbounds and saves a consistent mode-0600 SQLite
backup alongside it before setting panel credentials/path. The flag is one-time; normal
configure omits it. `panel.bootstrap: true` is also supported, but reset it to false afterwards.
`rotate exit/all` passes the flag after provisioning its replacement; `rotate bridge` does not.
Nonempty or unsupported databases fail the bootstrap guard. Bootstrap settings/service changes
are outside the managed-inbound transaction and need manual restoration from the saved DB on failure.

Before mutations, the controller writes `state/.world-transaction.json` with mode 0600 and
holds `.world-configure.lock`. Failed API calls are reconciled by listing actual state (including
lost responses after a successful mutation), then updated managed inbounds are restored by ID;
only newly created inbounds belonging to this transaction are deleted. `runtime.json` is published
atomically after checks. Apply still exits with failure after a successful rollback.

If rollback cannot finish, or the controller is interrupted, the journal remains. Restore access
to the same WORLD and rerun ordinary configure with its correct panel credentials/path: it first
restores the journal's state and exits with a recovery message. Run configure again to apply.
Do not delete the journal to bypass recovery. A journal for another host or an external conflicting
edit is rejected. RU configure and rotation are blocked while a journal is present. This is a
compensating API transaction, not a database-wide snapshot or a distributed RU/WORLD transaction;
in-progress sessions, traffic counters and concurrent manual panel changes are not covered.
Full DB/tfstate backups and live acceptance remain required by the sprint.

The supported endpoints are documented in the upstream [inbound controller](https://github.com/MHSanaei/3x-ui/blob/main/internal/web/controller/inbound.go)
and [server controller](https://github.com/MHSanaei/3x-ui/blob/main/internal/web/controller/server.go).
`validate --deep` uses the same access path: it opens an SSH tunnel to the panel's loopback
listener for `panel_login`/`panel_inbounds` and never dials the public `panel_url`.

## WORLD firewall: separate chain and direct policies

Every play that applies the firewall needs an explicit choice in `config/pipeline.yaml`:

```yaml
firewall:
  direct_policy: ""   # public | allowlist | closed — required, no default
  direct_cidrs: []    # allowlist sources (IPv4/IPv6)
  admin_cidrs: []     # extra SSH sources besides the controller
  ssh_world: false
  direct_only: false  # WORLD deliberately without a bridge
```

| Service | Allowed sources |
|---|---|
| Chain port (`xray.chain_port`) | bridge, controller |
| Direct ports (`reality_port`; `ss_port` TCP+UDP) | bridge, controller, plus `direct_policy` |
| SSH | controller, `admin_cidrs`, bridge reachability check; everyone with `ssh_world` |
| 3X-UI panel | nobody; API and UI go through SSH to loopback |

`closed` reproduces the earlier behaviour (clients only through the bridge); `public` keeps the
direct profiles usable as a control/fallback. A missing or invalid choice, a malformed CIDR, or a
CIDR covering the whole internet stops configure before inventory or Ansible. Full configure and
`--exit-firewall-only` require a bridge IP (Terraform output or `--bridge-ip` for a manual RU).
A WORLD without a bridge must be declared with `--direct-only` or `firewall.direct_only: true`;
that mode is refused when a bridge is known from Terraform, `--bridge-ip` or
`state/bridge_runtime.json`, and with `direct_policy: closed`.

The role builds the rule plan on the controller before touching ufw, then on WORLD: snapshots
the ufw rule files/defaults, arms a timer that restores that snapshot after
`exit_fw_safety_seconds` (300), forces `IPV6=yes`, rebuilds the rules, opens a fresh SSH session
from the controller, and verifies an active default-deny with INPUT policy DROP for IPv4 and,
when the host has IPv6, IPv6. Any failure restores the snapshot and fails the play; if the
controller cannot get back in, the timer performs the same restore (falling back to
`ufw disable` only when the snapshot is unusable). An unchanged re-apply records nothing.

```bash
pipelinectl configure --exit-firewall-only                     # re-apply rules only
pipelinectl configure --exit-firewall-rollback latest          # firewall before the last change
pipelinectl configure --exit-firewall-rollback 20261013T101500Z
pipelinectl configure --preflight                              # check inputs, touch nothing
```

Rollback restores a saved snapshot under the same guard and is repeatable; it does not need a
policy. The ten most recent snapshots are kept on WORLD in
`/var/lib/proxy-pipeline/exit-firewall/snapshots`, and the role prints their names. Firewall and
inbound rollbacks are separate steps: a failed firewall apply does not undo the already committed
WORLD inbound transaction, and `ufw reset` leaves the host unfiltered for the moment between
reset and enable. `rotate` runs `configure --preflight` before replacing nodes. Real ufw,
systemd and the live nodes have not been exercised yet.

## REALITY target measurements

```bash
pipelinectl probe-targets --purpose chain
```

This probes the candidates in `config/reality_targets.chain.yaml` from WORLD over SSH.
It dials `dest` with the configured SNI, verifies the certificate and TLS 1.3, offers
`expected_alpn` (default `h2`), and requires a matching negotiated protocol. Reports
include TCP/TLS/ALPN failure stages, error messages, peer IP and timings.

Trial results go to `state/chain_reality_target.json`; existing direct state stays in
`state/reality_target.json` (`--purpose direct`, the default). `--targets` overrides the
candidate file for either purpose. The new chain selection is measurement-only until
the separate chain identity is implemented: configure uses the direct target for fresh WORLD
identity only, and preserves the target of an existing WORLD.
A successful WORLD → target probe does not test RU → WORLD VLESS/REALITY connectivity.

## Local regression tests

```bash
python -m pytest -q tests
XRAY_TEST_BINARY=/absolute/path/to/xray python -m pytest -q tests/test_bridge_config.py
```

Use the activated development virtualenv (`pip install -e ".[dev]"`) so `ansible-playbook`
comes from the same environment; Ansible core 2.16+ is required. Tests exercise the real
Ansible install/apply tasks on temporary paths with simulated service commands: offline
delivery, version/hash mismatches, existing binary/unit preservation, repeat without restart,
failed validation/start and a failed second state write, including a fresh node rollback.
Firewall tests run the real role against emulated ufw/iptables/systemd/ssh that keep rule files
and the loaded ruleset apart: policy separation, refusals before any change, no-op repeat, failed
rule/SSH/IPv6 verification with restore, the armed guard after a lost session, and rollback.
WORLD tests inject API failures before/after commit, lost HTTP responses, failed state publication,
controller interruption, stale journals and conflicting edits. The local HTTP fixture checks
cookies/CSRF, and real Ansible runs the controller helper through a simulated SSH tunnel.
The real Xray fixture validates full WORLD configs and distinguishes its listeners from unrelated
processes. The actual 3X-UI binary and production SSH/API have not been exercised yet.

The optional binary needs `geoip.dat`, `geosite.dat` and the checked official
`Xray-linux-64.zip` beside it for the artifact test. The routing integration test starts two Xray
processes and an HTTP destination on loopback, checks the chain, stops the simulated
WORLD, and verifies that user requests cannot use RU's diagnostic direct route.
It also checks private destinations and unknown inbound tags. Public RU/WORLD servers
are not contacted. A second integration test runs the whole variant-2 data path on loopback:
a client Xray speaks VLESS + XHTTP to the RU entry, RU relays over VLESS + XHTTP + REALITY with a
separate chain identity to a WORLD config built by the managed-inbound code, and WORLD reaches
the target; a wrong client UUID, a wrong path, a private destination and a stopped WORLD all
yield no traffic. TLS termination by the reverse proxy, real networks and DNS/IPv6 acceptance
remain live tests. With `NGINX_TEST_BINARY` a third integration test puts the real nginx,
configured by the role's template, in front of that path: TLS for the bridge name, a 6 MB
download and upload compared bit for bit, a slow response that must arrive before it ends, the
plain-HTTP redirect and the answer to an unknown name. The packet-up variant is marked as an
expected failure for the reason in the table above.

CI downloads [Xray 25.10.15](https://github.com/XTLS/Xray-core/releases/tag/v25.10.15)
with a pinned SHA256 and runs these checks automatically. This is a regression fixture,
not the production version selection. Without `XRAY_TEST_BINARY`, eleven real-binary
tests are explicitly skipped; Ansible, helper, template and orchestrator tests still run.
