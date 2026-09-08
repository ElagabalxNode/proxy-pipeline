#!/usr/bin/env bash
#
# rotate-chain.sh — one-command rotation of the proxy chain (Yandex bridge + Cherry exit).
#
# Recreates the chosen instance(s) with new IPs/keys, re-probes the Reality target,
# reconfigures Xray/3X-UI, and re-exports client links + QR. Encodes every gotcha the
# manual runbook (docs/RUNBOOK-ROTATE-CHAIN.md) warns about so a rotation is one command:
# Cherry stock is checked before touching anything, the plan is refused if it would destroy
# anything outside the target (unrelated drift once recreated the bridge), and an Exit that
# an earlier failed rotation left missing from tfstate is simply created. The bridge sits on a
# reserved IP and its identity is restored from state/bridge_identity.json, so the client QR
# survives every rotation except an explicit `bridge --new-ip`.
#
# Usage:
#   scripts/rotate-chain.sh [all|exit|bridge] [--region SLUG] [--plan SLUG] [--new-ip] [--yes]
#
#   all     (default) recreate Exit + YC bridge VM (bridge keeps its reserved IP + identity)
#   exit             recreate only the Exit
#   bridge           recreate only the YC bridge VM (keeps its reserved IP + identity)
#   --new-ip         with bridge|all: ALSO replace the reserved address and drop the saved bridge
#                    identity -> the client QR changes. Without it the QR never changes.
#   --region SLUG    set exit_region in terraform.tfvars first (e.g. SE-Stockholm)
#   --plan   SLUG    set exit_plan   in terraform.tfvars first (e.g. B2-1-1gb-20s-shared)
#   --yes,-y         apply without the interactive confirm
#
# Safe to re-run. Refuses to proceed if anything looks wrong; prints the new QR path at the end.
set -euo pipefail

# ── args ───────────────────────────────────────────────────────────────────────
TARGET="all"; ASSUME_YES=0; REGION=""; PLAN=""; NEW_IP=0
[ $# -gt 0 ] && case "${1:-}" in all|exit|bridge) TARGET="$1"; shift;; esac
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y)  ASSUME_YES=1 ;;
    --new-ip)  NEW_IP=1 ;;
    --region)  REGION="${2:-}"; shift ;;
    --plan)    PLAN="${2:-}";   shift ;;
    all|exit|bridge) TARGET="$1" ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ "$NEW_IP" = 1 ] && [ "$TARGET" = exit ]; then echo "--new-ip applies to the bridge: use 'bridge' or 'all'" >&2; exit 2; fi
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TFDIR="$REPO/terraform"; TFVARS="$TFDIR/terraform.tfvars"
KEY="${PROXY_PIPELINE_SSH_KEY:-$HOME/.ssh/proxy_pipeline}"
cd "$REPO"

c() { printf '\033[1;36m[rotate] %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[rotate] %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m[rotate] ERROR: %s\033[0m\n' "$*" >&2; exit 1; }
tf() { terraform -chdir="$TFDIR" "$@"; }

# ── 1. environment: venv (system apt-ansible 2.10.8 is broken), secrets, YC auth ──
[ -f "$REPO/.venv/bin/activate" ] || die ".venv missing — system apt-ansible 2.10.8 is incompatible; create the venv first"
# shellcheck disable=SC1091
source "$REPO/.venv/bin/activate"
[ -f "$REPO/.env" ] && { set +u; # shellcheck disable=SC1091
  source "$REPO/.env"; set -u; }
# Yandex auth: prefer the service-account key file (long-lived) unless YC_TOKEN is set.
if [ -z "${YC_TOKEN:-}" ] && [ -f "$REPO/config/key.json" ]; then
  export YC_SERVICE_ACCOUNT_KEY_FILE="$REPO/config/key.json"
fi
[ -n "${YC_TOKEN:-}" ] || [ -n "${YC_SERVICE_ACCOUNT_KEY_FILE:-}" ] || \
  die "no Yandex auth — set YC_TOKEN or place the SA key at config/key.json"
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
if [ "$TARGET" != bridge ] && [ -n "${CHERRY_API_TOKEN:-}" ]; then
  STOCK="$(CHERRY_API_TOKEN="$CHERRY_API_TOKEN" REGION="$REGION_NOW" PLAN="$PLAN_NOW" python3 - <<'PY' 2>/dev/null || echo "?"
import json, os, urllib.request
tok, region, plan = os.environ["CHERRY_API_TOKEN"], os.environ["REGION"], os.environ["PLAN"]
def get(p):
    r = urllib.request.Request("https://api.cherryservers.com/v1" + p, headers={"Authorization": "Bearer " + tok})
    return json.load(urllib.request.urlopen(r, timeout=20))
for t in get("/teams"):
    for p in get(f"/teams/{t['id']}/plans"):
        if (p.get("slug") or p.get("name")) != plan: continue
        for ar in p.get("available_regions", []):
            if (ar.get("slug") or ar.get("name")) == region:
                print(ar.get("stock_qty", "?")); raise SystemExit
print("?")
PY
)"
  case "$STOCK" in
    "?") warn "could not check Cherry stock for $PLAN_NOW in $REGION_NOW — continuing blind" ;;
    0)   die "Cherry has NO stock for $PLAN_NOW in $REGION_NOW — pick another region/plan (--region NL-Amsterdam | LT-Siauliai …)" ;;
    *)   c "Cherry stock: $STOCK × $PLAN_NOW in $REGION_NOW" ;;
  esac
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
d = json.load(sys.stdin); print(d.get("bridge_vless", ""), d.get("bridge_shadowsocks", ""))' 2>/dev/null || true
}
BRIDGE_LINKS_BEFORE="$(bridge_links)"

# ── 5. terraform init + plan; refuse if anything OUTSIDE the target would be destroyed ──
# A full apply also applies unrelated drift. On 2026-09-08 a new Ubuntu image in the YC family
# made `rotate exit` silently recreate the bridge (new IP → all client QR dead). Never again:
# the plan may only delete/replace the resources this rotation targets.
c "terraform init"
tf init -input=false >/dev/null
PLANFILE="$TFDIR/.rotate.tfplan"
c "terraform plan ${REPLACE[*]}"
tf plan -input=false -out="$PLANFILE" "${REPLACE[@]}" >/dev/null
UNEXPECTED="$(tf show -json "$PLANFILE" | python3 -c '
import json, sys
allowed = set(sys.argv[1:])
for rc in json.load(sys.stdin).get("resource_changes", []):
    acts = rc.get("change", {}).get("actions", [])
    if "delete" in acts and rc["address"] not in allowed:
        print("  " + rc["address"] + "  ->  " + "/".join(acts))
' "${REPLACE[@]#-replace=}")"
if [ -n "$UNEXPECTED" ]; then
  rm -f "$PLANFILE"
  die "plan would destroy/replace resources OUTSIDE the rotation target '$TARGET':
$UNEXPECTED
Fix the drift first (lifecycle ignore_changes, or rotate 'all' deliberately)."
fi
tf show -no-color "$PLANFILE" | grep -E "^Plan:|will be (created|destroyed|replaced|updated)|must be replaced" || true
if [ "$ASSUME_YES" != 1 ]; then
  read -r -p "[rotate] apply this plan? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || { rm -f "$PLANFILE"; die "aborted by user"; }
fi
c "terraform apply (saved plan)"
tf apply -input=false "$PLANFILE"
rm -f "$PLANFILE"

EXIT_IP="$(tf output -raw exit_ip 2>/dev/null || true)"
BRIDGE_IP="$(tf output -raw bridge_ip 2>/dev/null || true)"
c "new Exit=$EXIT_IP  new Bridge=$BRIDGE_IP"
if [ "$NEW_IP" = 1 ]; then
  rm -f "$REPO/state/bridge_identity.json"
  c "--new-ip: dropped the saved bridge identity — the bridge role will generate a fresh one"
fi
# A rebuilt VM behind the same (reserved) IP has a new host key — drop stale known_hosts entries.
for h in "$EXIT_IP" "$BRIDGE_IP"; do [ -n "$h" ] && ssh-keygen -R "$h" >/dev/null 2>&1 || true; done

# ── 6. wait for the recreated node to be reachable ───────────────────────────────
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
if [ "$TARGET" = bridge ]; then
  c "waiting for new bridge SSH ($BRIDGE_IP) …"
  wait_ssh "$BRIDGE_IP" ubuntu || warn "bridge SSH not confirmed in ~10m (Ansible will still wait)"
else
  [ -n "$EXIT_IP" ] || die "terraform output exit_ip empty after apply"
  c "waiting for new Exit SSH + cloud-init ($EXIT_IP) …"
  wait_ssh "$EXIT_IP" root marker || die "Exit $EXIT_IP not ready (SSH/cloud-init) after ~10m"
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
  pipelinectl configure
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

# ── 10. summary ──────────────────────────────────────────────────────────────────
PANEL="$(tf output -raw panel_url 2>/dev/null || true)"
printf '\n\033[1;32m[rotate] DONE\033[0m\n'
echo   "  Exit:    ${EXIT_IP:-?}"
echo   "  Bridge:  ${BRIDGE_IP:-?}"
[ -n "$PANEL" ] && echo "  Panel:   $PANEL"
echo   "  Links:   $REPO/.client-links"
echo   "  QR:      $REPO/.client-links-qr/  ($QR_NOTE)"
echo
c "tip: verify the chain with 'pipelinectl validate --deep'"
