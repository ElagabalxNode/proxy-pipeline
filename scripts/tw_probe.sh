#!/bin/bash
# Timeweb region probe: deploy N ephemeral TW servers (terraform/probe), each runs xray
# (VLESS+Reality:443 + SS-2022:8388 + freedom) via cloud-init. From THIS host, test whether
# the VLESS+Reality TLS handshake traverses to each IP (openssl s_client to :443) and whether
# SS is reachable (:8388). Print a table + verdict, then destroy everything.
#
# Why openssl: a Reality port relays an unauthenticated TLS handshake to its dest
# (www.microsoft.com), so a completed TLS handshake == the (large) TLS round-trip to that IP
# works == VLESS+Reality is viable there. A timeout/reset == the path breaks TLS-Reality
# (what's happening on the current TW IP, while SS-2022 still passes).
#
# Requires: TWC_TOKEN exported (source .env), openssl, terraform. Costs money (~5 servers,
# hourly + IPv4) for the few minutes they live. The token must allow "delete via API
# without Telegram" or destroy will hang.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROBE="$ROOT/terraform/probe"
SNI="${PROBE_SNI:-www.microsoft.com}"
TLS_TIMEOUT=12
READY_TIMEOUT=240

[ -z "${TWC_TOKEN:-}" ] && { echo "ERROR: export TWC_TOKEN first (e.g. 'source .env')"; exit 1; }
command -v openssl >/dev/null || { echo "ERROR: openssl not found"; exit 1; }

cleanup() {
  echo
  echo "== destroy probe servers =="
  terraform -chdir="$PROBE" destroy -auto-approve -input=false
}
trap cleanup EXIT

echo "== deploy probe servers =="
terraform -chdir="$PROBE" init -input=false >/dev/null
terraform -chdir="$PROBE" apply -auto-approve -input=false \
  || echo "(apply reported errors for some regions — will probe whichever servers came up)"

# Read created servers from state (resilient to partial apply: some regions may lack capacity).
mapfile -t ROWS < <(terraform -chdir="$PROBE" show -json 2>/dev/null | python3 -c '
import json,sys
st=json.load(sys.stdin)
res=st.get("values",{}).get("root_module",{}).get("resources",[])
srv={}; ips={}
for r in res:
    if r.get("name")!="p": continue
    k=r.get("index")
    if r["type"]=="twc_server": srv[k]={"az":(r["values"].get("availability_zone") or "auto"),"loc":(r["values"].get("location") or "?")}
    if r["type"]=="twc_server_ip": ips[k]=r["values"].get("ip")
for k in sorted(ips):
    if ips[k]: print(k, ips[k], srv.get(k,{}).get("loc","?"), srv.get(k,{}).get("az","?"))
')

[ "${#ROWS[@]}" -eq 0 ] && { echo "no probe servers came up"; exit 1; }
echo "servers up: ${#ROWS[@]}"

echo "== wait for cloud-init/xray on :443 (up to ${READY_TIMEOUT}s) =="
deadline=$(( $(date +%s) + READY_TIMEOUT ))
for row in "${ROWS[@]}"; do
  set -- $row; ip=$2
  until timeout 5 bash -c "cat </dev/null >/dev/tcp/$ip/443" 2>/dev/null; do
    [ "$(date +%s)" -ge "$deadline" ] && { echo "  $ip :443 not up in time (cloud-init slow?)"; break; }
    sleep 5
  done
done
sleep 8  # let xray finish binding after the port opens

echo
printf "%-9s %-16s %-7s %-7s %-9s %-5s\n" LABEL IP LOC AZ REALITY SS
printf "%-9s %-16s %-7s %-7s %-9s %-5s\n" ----- -- --- -- ------- --
ok_count=0
for row in "${ROWS[@]}"; do
  set -- $row; label=$1; ip=$2; loc=$3; az=$4
  if timeout "$TLS_TIMEOUT" openssl s_client -connect "$ip:443" -servername "$SNI" -brief </dev/null 2>&1 \
       | grep -qiE "Protocol version|CONNECTION ESTABLISHED"; then
    reality="OK"; ok_count=$((ok_count+1))
  else
    reality="FAIL"
  fi
  if timeout 6 bash -c "cat </dev/null >/dev/tcp/$ip/8388" 2>/dev/null; then ss="OK"; else ss="FAIL"; fi
  printf "%-9s %-16s %-7s %-7s %-9s %-5s\n" "$label" "$ip" "$loc" "$az" "$reality" "$ss"
done

echo
if [ "$ok_count" -gt 0 ]; then
  echo "VERDICT: $ok_count/${#ROWS[@]} Timeweb IP(s) carry VLESS+Reality (TLS traverses)."
  echo "         -> Timeweb IS viable for Reality; use a passing region/IP for the bridge."
else
  echo "VERDICT: 0/${#ROWS[@]} Timeweb IPs carried VLESS+Reality (TLS blocked on all)."
  echo "         -> Drop Timeweb for VLESS+Reality. Options: SS-2022 on Timeweb (works), or keep Yandex."
fi
echo "(Confirm any winner with a real client; this host is one vantage.)"
