#!/usr/bin/env bash
#
# rotate-chain.sh — one-command rotation of the proxy chain (Yandex bridge + Cherry exit).
#
# Recreates the chosen instance(s) with new IPs/keys, re-probes the Reality target,
# reconfigures Xray/3X-UI, and re-exports client links + QR. Encodes every gotcha the
# manual runbook (docs/RUNBOOK-ROTATE-CHAIN.md) warns about so a rotation is one command.
#
# Usage:
#   scripts/rotate-chain.sh [all|exit|bridge] [--region SLUG] [--plan SLUG] [--yes]
#
#   all     (default) recreate Exit + YC bridge
#   exit             recreate only the Exit
#   bridge           recreate only the YC bridge
#   --region SLUG    set exit_region in terraform.tfvars first (e.g. SE-Stockholm)
#   --plan   SLUG    set exit_plan   in terraform.tfvars first (e.g. B2-1-1gb-20s-shared)
#   --yes,-y         terraform apply -auto-approve (default: interactive confirm)
#
# Safe to re-run. Refuses to proceed if anything looks wrong; prints the new QR path at the end.
set -euo pipefail

# ── args ───────────────────────────────────────────────────────────────────────
TARGET="all"; ASSUME_YES=0; REGION=""; PLAN=""
[ $# -gt 0 ] && case "${1:-}" in all|exit|bridge) TARGET="$1"; shift;; esac
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y)  ASSUME_YES=1 ;;
    --region)  REGION="${2:-}"; shift ;;
    --plan)    PLAN="${2:-}";   shift ;;
    all|exit|bridge) TARGET="$1" ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

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

# ── 4. resolve replace targets from live state (provider/index agnostic) ──────────
EXIT_ADDR="$(tf state list 2>/dev/null | grep -E 'cherryservers_server\.exit$' | head -1 || true)"
BRIDGE_ADDR="$(tf state list 2>/dev/null | grep -E 'yandex_compute_instance\.bridge$' | head -1 || true)"
REPLACE=()
case "$TARGET" in
  exit)   [ -n "$EXIT_ADDR" ]   || die "Exit not found in tfstate";   REPLACE=(-replace="$EXIT_ADDR") ;;
  bridge) [ -n "$BRIDGE_ADDR" ] || die "YC bridge not found in tfstate"; REPLACE=(-replace="$BRIDGE_ADDR") ;;
  all)
    [ -n "$EXIT_ADDR" ]   && REPLACE+=(-replace="$EXIT_ADDR")
    [ -n "$BRIDGE_ADDR" ] && REPLACE+=(-replace="$BRIDGE_ADDR")
    [ ${#REPLACE[@]} -gt 0 ] || die "neither Exit nor bridge found in tfstate" ;;
esac

# ── 5. terraform init + targeted -replace apply (full apply → outputs refresh) ────
c "terraform init"
tf init -input=false >/dev/null
c "terraform apply ${REPLACE[*]}"
APPLY=(-input=false "${REPLACE[@]}")
[ "$ASSUME_YES" = 1 ] && APPLY+=(-auto-approve)
tf apply "${APPLY[@]}"

EXIT_IP="$(tf output -raw exit_ip 2>/dev/null || true)"
BRIDGE_IP="$(tf output -raw bridge_ip 2>/dev/null || true)"
c "new Exit=$EXIT_IP  new Bridge=$BRIDGE_IP"

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

# ── 10. summary ──────────────────────────────────────────────────────────────────
PANEL="$(tf output -raw panel_url 2>/dev/null || true)"
printf '\n\033[1;32m[rotate] DONE\033[0m\n'
echo   "  Exit:    ${EXIT_IP:-?}"
echo   "  Bridge:  ${BRIDGE_IP:-?}"
[ -n "$PANEL" ] && echo "  Panel:   $PANEL"
echo   "  Links:   $REPO/.client-links"
echo   "  QR:      $REPO/.client-links-qr/  → give clients: bridge_vless.png"
echo
c "tip: verify the chain with 'pipelinectl validate --deep'"
