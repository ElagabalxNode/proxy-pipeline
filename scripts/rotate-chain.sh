#!/usr/bin/env bash
#
# rotate-chain.sh — one-command rotation of the proxy chain (Cherry Exit + RU bridge).
#
# Recreates the chosen instance(s) with new IPs/keys, re-probes the Reality target,
# reconfigures Xray/3X-UI, and re-exports client links + QR. Encodes every gotcha the
# manual runbook (docs/RUNBOOK-ROTATE-CHAIN.md) warns about so a rotation is one command:
# Cherry stock is checked before touching anything, the plan is refused if it would destroy
# anything outside the target (unrelated drift once recreated the bridge), and an Exit that
# an earlier failed rotation left missing from tfstate is simply created. The bridge sits on a
# reserved IP and its identity is restored from state/bridge_identity.json, so the client QR
# survives every rotation except an explicit `bridge --new-ip`. A new Exit must answer TCP from
# the bridge (RU-side IP blocks pass ICMP but drop TCP); if not, the Exit is re-rolled for another
# IP up to ROTATE_MAX_REROLLS (4) times and each blocked IP is logged to state/blocked_exit_ips.log.
#
# A RU bridge outside Terraform (bridge_manual in config/pipeline.yaml, e.g. VDSina) is not
# recreated here: `bridge --bridge-ip NEW` moves it onto a server the operator has already
# created (SSH key in place), keeps its identity, moves the DNS name and updates bridge_manual.ip,
# so the client link survives. `exit` reconfigures that bridge onto the new Exit as usual.
#
# Usage:
#   scripts/rotate-chain.sh [all|exit|bridge] [--region SLUG] [--plan SLUG] [--new-ip] [--yes]
#   scripts/rotate-chain.sh bridge --bridge-ip NEW_IP        # manual RU bridge (no Terraform)
#
#   all     (default) recreate Exit + YC bridge VM (bridge keeps its reserved IP + identity)
#   exit             recreate only the Exit
#   bridge           recreate only the YC bridge VM (keeps its reserved IP + identity),
#                    or with --bridge-ip move a manual bridge to that server
#   --new-ip         with bridge|all: ALSO replace the reserved address and drop the saved bridge
#                    identity -> the client QR changes. Without it the QR never changes.
#   --region SLUG    set exit_region in terraform.tfvars first (e.g. SE-Stockholm)
#   --plan   SLUG    set exit_plan   in terraform.tfvars first (e.g. B2-1-1gb-20s-shared)
#   --yes,-y         apply without the interactive confirm
#
# Safe to re-run. Refuses to proceed if anything looks wrong; prints the new QR path at the end.
set -euo pipefail

# ── args ───────────────────────────────────────────────────────────────────────
TARGET="all"; ASSUME_YES=0; REGION=""; PLAN=""; NEW_IP=0; NEW_BRIDGE_IP=""
[ $# -gt 0 ] && case "${1:-}" in all|exit|bridge) TARGET="$1"; shift;; esac
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y)  ASSUME_YES=1 ;;
    --new-ip)  NEW_IP=1 ;;
    --region)  REGION="${2:-}"; shift ;;
    --plan)    PLAN="${2:-}";   shift ;;
    --bridge-ip) NEW_BRIDGE_IP="${2:-}"; shift ;;
    all|exit|bridge) TARGET="$1" ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ "$NEW_IP" = 1 ] && [ "$TARGET" = exit ]; then echo "--new-ip applies to the bridge: use 'bridge' or 'all'" >&2; exit 2; fi
if [ -n "$NEW_BRIDGE_IP" ] && [ "$TARGET" != bridge ]; then echo "--bridge-ip applies to 'bridge' only" >&2; exit 2; fi
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TFDIR="$REPO/terraform"; TFVARS="$TFDIR/terraform.tfvars"
KEY="${PROXY_PIPELINE_SSH_KEY:-$HOME/.ssh/proxy_pipeline}"
cd "$REPO"

c() { printf '\033[1;36m[rotate] %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[rotate] %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m[rotate] ERROR: %s\033[0m\n' "$*" >&2; exit 1; }
tf() { terraform -chdir="$TFDIR" "$@"; }
manual_bridge() { # "ip user" of bridge_manual in config/pipeline.yaml, or nothing
  python3 - "$REPO/config/pipeline.yaml" <<'PY'
import sys, yaml
m = (yaml.safe_load(open(sys.argv[1])) or {}).get("bridge_manual") or {}
if isinstance(m, dict) and str(m.get("ip") or "").strip():
    print(str(m["ip"]).strip(), str(m.get("ssh_user") or "root").strip())
PY
}
set_manual_bridge_ip() { # rewrite bridge_manual.ip in place, keeping the rest of the file
  python3 - "$REPO/config/pipeline.yaml" "$1" <<'PY'
import re, sys
path, ip = sys.argv[1], sys.argv[2]
text = open(path).read()
new, n = re.subn(r"(?m)^(bridge_manual:[ \t]*\n(?:[ \t]*(?:#.*)?\n|[ \t]+(?!ip:).*\n)*?[ \t]+ip:[ \t]*)\S+",
                 lambda m: m.group(1) + ip, text, count=1)
if n != 1:
    sys.exit("bridge_manual.ip not found in " + path)
open(path, "w").write(new)
PY
}
wait_ssh() { # host user [need_marker]
  local host="$1" user="$2" marker="${3:-}" i
  for i in $(seq 1 60); do
    if [ -n "$marker" ]; then
      ssh -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=8 -o BatchMode=yes "$user@$host" 'test -f /opt/proxy-pipeline/.status' 2>/dev/null && return 0
    else
      ssh -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=8 -o BatchMode=yes "$user@$host" 'true' 2>/dev/null && return 0
    fi
    sleep 10
  done
  return 1
}

# Keep the original WORLD reachable until its API transaction has been recovered.
if [ -e "$REPO/state/.world-transaction.json" ] || [ -L "$REPO/state/.world-transaction.json" ]; then
  die "unfinished WORLD transaction: recover with configure on the original Exit before rotating"
fi

# ── 0. single instance: the watchdog may fire while an operator rotates by hand ────────
exec 9>"$REPO/state/.rotate.lock"
flock -n 9 || die "another rotation is already running (state/.rotate.lock is held)"

# ── 1. environment: venv (system apt-ansible 2.10.8 is broken), secrets, YC auth ──
[ -f "$REPO/.venv/bin/activate" ] || die ".venv missing — system apt-ansible 2.10.8 is incompatible; create the venv first"
# shellcheck disable=SC1091
source "$REPO/.venv/bin/activate"
[ -f "$REPO/.env" ] && { set +u; # shellcheck disable=SC1091
  source "$REPO/.env"; set -u; }
# Yandex auth: prefer the service-account key file (long-lived) unless YC_TOKEN is set. Needed
# only while the Yandex bridge module is enabled.
if [ -z "${YC_TOKEN:-}" ] && [ -f "$REPO/config/key.json" ]; then
  export YC_SERVICE_ACCOUNT_KEY_FILE="$REPO/config/key.json"
fi
if grep -qE '^[[:space:]]*enable_yandex_bridge[[:space:]]*=[[:space:]]*true' "$TFVARS"; then
  [ -n "${YC_TOKEN:-}" ] || [ -n "${YC_SERVICE_ACCOUNT_KEY_FILE:-}" ] || \
    die "no Yandex auth — set YC_TOKEN or place the SA key at config/key.json"
fi
case "$(command -v ansible-playbook || true)" in
  "$REPO/.venv/"*) : ;;
  *) die "ansible-playbook is not from .venv (got '$(command -v ansible-playbook || echo none)') — venv activation failed" ;;
esac
command -v pipelinectl >/dev/null || die "pipelinectl not on PATH after venv activation"

# ── 2. optional region/plan patch into tfvars ────────────────────────────────────
[ -n "$REGION" ] && { sed -i -E "s|^([[:space:]]*exit_region[[:space:]]*=[[:space:]]*).*|\1\"$REGION\"|" "$TFVARS"; c "exit_region -> $REGION"; }
[ -n "$PLAN" ]   && { sed -i -E "s|^([[:space:]]*exit_plan[[:space:]]*=[[:space:]]*).*|\1\"$PLAN\"|"     "$TFVARS"; c "exit_plan -> $PLAN"; }

# ── 3. guards ────────────────────────────────────────────────────────────────────
REGION_NOW="$(grep -oE '^[[:space:]]*exit_region[[:space:]]*=[[:space:]]*"[^"]*"' "$TFVARS" | grep -oE '"[^"]*"' | tr -d '"' || true)"
PLAN_NOW="$(grep -oE '^[[:space:]]*exit_plan[[:space:]]*=[[:space:]]*"[^"]*"' "$TFVARS" | grep -oE '"[^"]*"' | tr -d '"' || true)"
c "region=$REGION_NOW  plan=$PLAN_NOW  target=$TARGET"

# Cherry sells Gen-1 (B1-*) cloud VPS only in LT/NL/US/SG; DE-Frankfurt, SE-Stockholm,
# JP-Tokyo carry only Gen-2 (B2-*). Catch the 'Plan not found' trap before apply.
if printf '%s' "$REGION_NOW" | grep -qE 'Frankfurt|Stockholm|Tokyo' && printf '%s' "$PLAN_NOW" | grep -qE '^B1-'; then
  die "exit_plan '$PLAN_NOW' (Gen-1) is not sold in $REGION_NOW — use a B2-* plan (e.g. B2-1-1gb-20s-shared)"
fi
# Timeweb was abandoned; stale twc_* in state poisons rotation (wrong chain mode / dead target).
if tf state list 2>/dev/null | grep -q 'twc_'; then
  die "Timeweb resources still in tfstate — purge them first (see runbook Фаза 1: terraform state rm 'module.bridge_timeweb[0]...')"
fi
# Cherry stock: an out-of-stock region fails only at create time (2026-09-08: SE-Stockholm had 0,
# the API answered 400 "Server is out of stock" after the old Exit was already gone). Ask first.
if [ "$TARGET" != bridge ]; then
  STOCK="$(pipelinectl stock --plan "$PLAN_NOW" --region "$REGION_NOW" --format qty 2>/dev/null || echo "?")"
  case "$STOCK" in
    "?") warn "could not check Cherry stock for $PLAN_NOW in $REGION_NOW — continuing blind" ;;
    0)   die "Cherry has NO stock for $PLAN_NOW in $REGION_NOW — run 'pipelinectl stock' and pick another --region/--plan" ;;
    *)   c "Cherry stock: $STOCK × $PLAN_NOW in $REGION_NOW" ;;
  esac
fi

# ── 3b. manual RU bridge (outside Terraform): move it to the server given by --bridge-ip ──
read -r MANUAL_IP MANUAL_USER <<<"$(manual_bridge)" || true
if [ "$TARGET" = bridge ] && ! tf state list 2>/dev/null | grep -qE 'yandex_compute_instance\.bridge$'; then
  [ -n "$MANUAL_IP" ] || [ -n "$NEW_BRIDGE_IP" ] || die "no bridge in Terraform and no bridge_manual in config/pipeline.yaml"
  [ -n "$NEW_BRIDGE_IP" ] || die "the bridge is not managed by Terraform: create a new RU server with your SSH key, then run: pipelinectl rotate bridge --bridge-ip NEW_IP"
  [ "$NEW_IP" = 1 ] && die "--new-ip is for the Yandex bridge; a manual bridge keeps its identity (delete state/bridge_identity.json for a new one)"
  BRIDGE_USER="${MANUAL_USER:-root}"
  c "manual bridge: ${MANUAL_IP:-none} -> $NEW_BRIDGE_IP (user $BRIDGE_USER)"
  pipelinectl configure --preflight --bridge-only --bridge-ip "$NEW_BRIDGE_IP" --bridge-user "$BRIDGE_USER" \
    || die "configure preflight failed — nothing was changed"
  BRIDGE_LINKS_BEFORE="$(pipelinectl links --no-export --no-qr --format json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("bridge_xhttp",""))' 2>/dev/null || true)"
  ssh-keygen -R "$NEW_BRIDGE_IP" >/dev/null 2>&1 || true
  c "waiting for SSH on the new bridge ($NEW_BRIDGE_IP) …"
  wait_ssh "$NEW_BRIDGE_IP" "$BRIDGE_USER" || die "no SSH to $BRIDGE_USER@$NEW_BRIDGE_IP — is the controller key in authorized_keys?"
  c "configure --bridge-only on the new server (identity and certificate from state/)"
  pipelinectl configure --bridge-only --bridge-ip "$NEW_BRIDGE_IP" --bridge-user "$BRIDGE_USER" \
    || die "configure of the new bridge failed — DNS and config still point to the old bridge"
  if python3 -c 'import sys, yaml; sys.exit(0 if ((yaml.safe_load(open(sys.argv[1])) or {}).get("bridge_entry") or {}).get("domain") else 1)' "$REPO/config/pipeline.yaml"; then
    c "dns ensure --ip $NEW_BRIDGE_IP --replace (clients follow the name)"
    pipelinectl dns ensure --ip "$NEW_BRIDGE_IP" --replace \
      || die "the new bridge works but DNS was not moved — run: pipelinectl dns ensure --ip $NEW_BRIDGE_IP --replace"
  fi
  set_manual_bridge_ip "$NEW_BRIDGE_IP" || die "could not write bridge_manual.ip — set it to $NEW_BRIDGE_IP by hand"
  c "bridge_manual.ip -> $NEW_BRIDGE_IP"
  EXIT_IP="$(tf output -raw exit_ip 2>/dev/null || true)"
  # An active Exit firewall admits the chain port only from the bridge: let the new one in.
  if [ -n "$EXIT_IP" ] && ssh -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes root@"$EXIT_IP" \
      'ufw status 2>/dev/null | grep -q "Status: active"' 2>/dev/null; then
    c "Exit firewall is active — configure --exit-firewall-only (chain port for the new bridge)"
    pipelinectl configure --exit-firewall-only || warn "Exit firewall not updated — run: pipelinectl configure --exit-firewall-only"
  fi
  pipelinectl links --export --qr >/dev/null
  BRIDGE_LINKS_AFTER="$(pipelinectl links --no-export --no-qr --format json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("bridge_xhttp",""))' 2>/dev/null || true)"
  FINAL_OK=1
  pipelinectl validate --deep || { FINAL_OK=0; warn "END-TO-END CHECK FAILED (see [FAIL] above)"; }
  if [ "$FINAL_OK" = 1 ]; then printf '\n\033[1;32m[rotate] DONE\033[0m\n'; else printf '\n\033[1;31m[rotate] DONE — WITH FAILURES (see above)\033[0m\n'; fi
  echo "  Bridge:  $NEW_BRIDGE_IP (was ${MANUAL_IP:-none}) — delete the old server once clients work"
  if [ -n "$BRIDGE_LINKS_BEFORE" ] && [ "$BRIDGE_LINKS_BEFORE" = "$BRIDGE_LINKS_AFTER" ]; then
    echo "  Client link: UNCHANGED — clients keep working after their DNS cache expires (TTL 10 min)"
  else
    warn "client link CHANGED — re-issue it from $REPO/.client-links"
  fi
  [ "$FINAL_OK" = 1 ] || exit 1
  exit 0
fi

# ── 4. resolve replace targets from live state (provider/index agnostic) ──────────
EXIT_ADDR="$(tf state list 2>/dev/null | grep -E 'cherryservers_server\.exit$' | head -1 || true)"
BRIDGE_ADDR="$(tf state list 2>/dev/null | grep -E 'yandex_compute_instance\.bridge$' | head -1 || true)"
ADDRESS_ADDR="$(tf state list 2>/dev/null | grep -E 'yandex_vpc_address\.bridge$' | head -1 || true)"
REPLACE=()
case "$TARGET" in
  exit)
    if [ -n "$EXIT_ADDR" ]; then REPLACE=(-replace="$EXIT_ADDR")
    else warn "Exit is not in tfstate (an earlier rotation destroyed it and the create failed?) — it will be CREATED, nothing replaced"; fi ;;
  bridge) [ -n "$BRIDGE_ADDR" ] || die "YC bridge not found in tfstate"; REPLACE=(-replace="$BRIDGE_ADDR") ;;
  all)
    [ -n "$EXIT_ADDR" ]   && REPLACE+=(-replace="$EXIT_ADDR")
    [ -n "$BRIDGE_ADDR" ] && REPLACE+=(-replace="$BRIDGE_ADDR")
    [ ${#REPLACE[@]} -gt 0 ] || die "neither Exit nor bridge found in tfstate" ;;
esac
# The bridge's reserved address is never touched unless --new-ip asks for it (the plan guard in
# step 5 enforces that); with --new-ip it is replaced too and the saved identity is dropped after
# a successful apply, so clients get a deliberately new QR.
if [ "$NEW_IP" = 1 ]; then
  if [ -n "$ADDRESS_ADDR" ]; then
    REPLACE+=(-replace="$ADDRESS_ADDR"); c "--new-ip: the reserved bridge address will be replaced too — client QR WILL change"
  else
    warn "--new-ip: no yandex_vpc_address.bridge in tfstate — bridge IP is ephemeral and changes anyway"
  fi
fi

# ── 4b. snapshot bridge client links — an exit-only rotation must leave them intact ──
# (the bridge role reuses the identity it already serves; step 9 verifies that it did)
bridge_links() {
  pipelinectl links --no-export --no-qr --format json 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin); print(d.get("bridge_xhttp", ""), d.get("bridge_vless", ""), d.get("bridge_shadowsocks", ""))' 2>/dev/null || true
}
BRIDGE_LINKS_BEFORE="$(bridge_links)"

# ── 5. terraform init + plan; refuse if anything OUTSIDE the target would be destroyed ──
# A full apply also applies unrelated drift. On 2026-09-08 a new Ubuntu image in the YC family
# made `rotate exit` silently recreate the bridge (new IP → all client QR dead). Never again:
# the plan may only delete/replace the resources this rotation targets.
c "terraform init"
tf init -input=false >/dev/null
# Configuration inputs are checked while the old node still exists: a missing Xray pin,
# firewall.direct_policy or panel password, or a WORLD whose bridge is not known to Terraform
# (manual RU / direct-only not declared) would otherwise fail configure after the replacement.
PREFLIGHT=(--preflight); [ "$TARGET" = bridge ] && PREFLIGHT+=(--bridge-only)
c "configure ${PREFLIGHT[*]}"
pipelinectl configure "${PREFLIGHT[@]}" || die "configure preflight failed — nothing was replaced; fix the configuration first"
# A new Exit installs the 3X-UI archive pinned in terraform.tfvars and configure then demands the
# Xray pinned in xray.world_release. Prove on the controller that both describe one artifact: a
# wrong checksum found by cloud-init or configure would be found after the old Exit is gone.
if [ "$TARGET" != bridge ]; then
  c "world-release --verify (pinned 3X-UI archive + bundled Xray)"
  pipelinectl world-release --verify || die "WORLD release pin not verified — nothing was replaced"
fi
PLANFILE="$TFDIR/.rotate.tfplan"
trap 'rm -f "$PLANFILE"' EXIT   # never leave a plan (it embeds secrets) behind, however we exit

plan_and_apply() { # confirm|auto — plans REPLACE[], runs the guard, (asks,) applies the saved plan
  local mode="$1" unexpected ans
  c "terraform plan ${REPLACE[*]}"
  tf plan -input=false -out="$PLANFILE" "${REPLACE[@]}" >/dev/null
  unexpected="$(tf show -json "$PLANFILE" | python3 -c '
import json, sys
allowed = set(sys.argv[1:])
for rc in json.load(sys.stdin).get("resource_changes", []):
    acts = rc.get("change", {}).get("actions", [])
    if "delete" in acts and rc["address"] not in allowed:
        print("  " + rc["address"] + "  ->  " + "/".join(acts))
' "${REPLACE[@]#-replace=}")"
  if [ -n "$unexpected" ]; then
    rm -f "$PLANFILE"
    die "plan would destroy/replace resources OUTSIDE the rotation target '$TARGET':
$unexpected
Fix the drift first (lifecycle ignore_changes, or rotate 'all' deliberately)."
  fi
  tf show -no-color "$PLANFILE" | grep -E "^Plan:|will be (created|destroyed|replaced|updated)|must be replaced" || true
  if [ "$mode" = confirm ] && [ "$ASSUME_YES" != 1 ]; then
    # `|| ans=""`: at EOF (no tty, e.g. a bot without --yes) read returns 1 and set -e would kill
    # the script silently — treat it as "no" and say so.
    read -r -p "[rotate] apply this plan? [y/N] " ans || ans=""
    [[ "$ans" =~ ^[Yy]$ ]] || die "aborted (no confirmation; pass --yes for unattended runs)"
  fi
  c "terraform apply (saved plan)"
  tf apply -input=false "$PLANFILE"
  rm -f "$PLANFILE"
}
plan_and_apply confirm

EXIT_IP="$(tf output -raw exit_ip 2>/dev/null || true)"
BRIDGE_IP="$(tf output -raw bridge_ip 2>/dev/null || true)"
BRIDGE_USER="$(tf output -raw bridge_ssh_user 2>/dev/null || true)"; BRIDGE_USER="${BRIDGE_USER:-ubuntu}"
if [ -n "$MANUAL_IP" ]; then BRIDGE_IP="$MANUAL_IP"; BRIDGE_USER="${MANUAL_USER:-root}"; fi
c "new Exit=$EXIT_IP  new Bridge=$BRIDGE_IP"
if [ "$NEW_IP" = 1 ]; then
  rm -f "$REPO/state/bridge_identity.json"
  c "--new-ip: dropped the saved bridge identity — the bridge role will generate a fresh one"
fi
# A rebuilt VM behind the same (reserved) IP has a new host key — drop stale known_hosts entries.
for h in "$EXIT_IP" "$BRIDGE_IP"; do [ -n "$h" ] && ssh-keygen -R "$h" >/dev/null 2>&1 || true; done

# ── 6. wait for the recreated node(s); the Exit must also answer TCP FROM THE BRIDGE ──────
# 2026-09-11: a fresh Cherry DE IP (84.32.98.23) answered TCP from this host but not one SYN from
# the YC bridge reached it (ICMP did) — RU-side block of a recycled VPN range. Configuring the
# bridge onto such an Exit takes the chain down for every client, so test TCP from the bridge
# (port 22 is up first) and re-roll the Exit until one is reachable. ICMP would not catch it.
bridge_can_reach_exit() {
  ssh -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes "$BRIDGE_USER@$BRIDGE_IP" \
    "for i in 1 2 3; do timeout 5 bash -c 'cat </dev/null >/dev/tcp/$EXIT_IP/22' 2>/dev/null && exit 0; sleep 3; done; exit 1"
}
MAX_REROLLS="${ROTATE_MAX_REROLLS:-4}"
if [ "$TARGET" = bridge ]; then
  c "waiting for new bridge SSH ($BRIDGE_IP) …"
  wait_ssh "$BRIDGE_IP" "$BRIDGE_USER" || warn "bridge SSH not confirmed in ~10m (Ansible will still wait)"
else
  if [ "$TARGET" = all ] && [ -n "$BRIDGE_IP" ] && [ -z "$MANUAL_IP" ]; then
    c "waiting for new bridge SSH ($BRIDGE_IP) …"
    wait_ssh "$BRIDGE_IP" "$BRIDGE_USER" || warn "bridge SSH not confirmed in ~10m"
  fi
  attempt=1
  while :; do
    [ -n "$EXIT_IP" ] || die "terraform output exit_ip empty after apply"
    c "waiting for new Exit SSH ($EXIT_IP) …"
    wait_ssh "$EXIT_IP" root || die "Exit $EXIT_IP: no SSH after ~10m"
    if [ -z "$BRIDGE_IP" ]; then warn "no bridge — skipping bridge→Exit reachability check"; break; fi
    c "checking TCP reachability of the new Exit FROM the bridge ($BRIDGE_IP → $EXIT_IP:22) …"
    if bridge_can_reach_exit; then
      c "bridge → Exit TCP OK"
      # Recycled address? 2026-09-12: a fresh Exit inherited an IP whose previous tenant ran a
      # malware C2 — the provider null-routed it within a day. InternetDB still shows that tenant.
      rc=0; REP="$(pipelinectl ipcheck "$EXIT_IP" 2>&1)" || rc=$?
      case "$rc" in
        0) c "IP reputation: $REP"; break ;;
        1) reason="dirty recycled IP — $REP" ;;
        *) warn "IP reputation check unavailable ($REP) — continuing without it"; break ;;
      esac
    else
      reason="not reachable over TCP from the bridge while this host reaches it (RU-side block)"
    fi
    echo "$(date -u +%FT%TZ) $EXIT_IP region=$REGION_NOW attempt=$attempt $reason" >> "$REPO/state/blocked_exit_ips.log"
    warn "Exit $EXIT_IP rejected: $reason (logged to state/blocked_exit_ips.log)"
    if [ "$attempt" -ge "$MAX_REROLLS" ]; then
      die "$attempt Exits in a row rejected (RU-blocked or dirty) — this region's pool looks burned; try another --region (see 'pipelinectl stock') or another provider"
    fi
    attempt=$((attempt + 1))
    c "re-rolling the Exit for a different IP (attempt $attempt/$MAX_REROLLS) …"
    EXIT_ADDR="$(tf state list 2>/dev/null | grep -E 'cherryservers_server\.exit$' | head -1 || true)"
    [ -n "$EXIT_ADDR" ] || die "Exit vanished from tfstate during re-roll"
    REPLACE=(-replace="$EXIT_ADDR")
    plan_and_apply auto   # same shape as the plan already confirmed: replace the Exit only
    ssh-keygen -R "$EXIT_IP" >/dev/null 2>&1 || true
    EXIT_IP="$(tf output -raw exit_ip 2>/dev/null || true)"
    c "new Exit=$EXIT_IP"
  done
  c "waiting for Exit cloud-init to finish ($EXIT_IP) …"
  wait_ssh "$EXIT_IP" root marker || die "Exit $EXIT_IP not ready (cloud-init marker) after ~10m"
fi

# ── 7. re-probe Reality target (exit/all only); keep a backup of the last good one ─
if [ "$TARGET" != bridge ]; then
  [ -f state/reality_target.json ] && cp -f state/reality_target.json state/reality_target.json.bak || true
  c "probe-targets (best Reality SNI for the new Exit)"
  if ! pipelinectl probe-targets; then
    [ -f state/reality_target.json.bak ] && cp -f state/reality_target.json.bak state/reality_target.json
    die "probe-targets failed — restored previous reality_target.json; not reconfiguring"
  fi
fi

# ── 8. configure (full = Exit+Bridge; bridge-only never touches the Exit) ─────────
if [ "$TARGET" = bridge ]; then
  c "configure --bridge-only"
  pipelinectl configure --bridge-only
else
  c "configure (Exit role → Bridge role)"
  # exit/all provisioned a replacement WORLD; initialize its empty panel once.
  pipelinectl configure --bootstrap-panel
fi

# ── 9. links + QR ────────────────────────────────────────────────────────────────
c "export client links + QR"
pipelinectl links --export --qr >/dev/null
BRIDGE_LINKS_AFTER="$(bridge_links)"
if [ "$NEW_IP" = 1 ]; then
  QR_NOTE="NEW (--new-ip) → give clients: bridge_vless.png"
elif [ -n "$BRIDGE_LINKS_BEFORE" ] && [ "$BRIDGE_LINKS_BEFORE" = "$BRIDGE_LINKS_AFTER" ]; then
  QR_NOTE="UNCHANGED — clients keep working, nothing to hand out"
else
  warn "bridge client links CHANGED without --new-ip — clients must re-import bridge_vless.png"
  QR_NOTE="CHANGED (unexpected) → re-issue bridge_vless.png"
fi

# ── 9b. end-to-end verification from the bridge's vantage — the leg clients actually use ──
# `validate --deep` now runs one chain-probe round ON the bridge (hop, whole chain, bridge egress,
# Reality dest). A rotation that "completed" but does not carry traffic must not end green.
c "validate --deep (incl. one chain-probe round on the bridge)"
FINAL_OK=1
pipelinectl validate --deep || { FINAL_OK=0; warn "END-TO-END CHECK FAILED — the chain is configured but something does not work (see [FAIL] above)"; }

# ── 10. summary ──────────────────────────────────────────────────────────────────
PANEL="$(tf output -raw panel_url 2>/dev/null || true)"
if [ "$FINAL_OK" = 1 ]; then printf '\n\033[1;32m[rotate] DONE\033[0m\n'; else printf '\n\033[1;31m[rotate] DONE — WITH FAILURES (see above)\033[0m\n'; fi
echo   "  Exit:    ${EXIT_IP:-?}"
echo   "  Bridge:  ${BRIDGE_IP:-?}"
[ -n "$PANEL" ] && echo "  Panel:   $PANEL"
echo   "  Links:   $REPO/.client-links"
echo   "  QR:      $REPO/.client-links-qr/  ($QR_NOTE)"
echo
[ "$FINAL_OK" = 1 ] || exit 1
