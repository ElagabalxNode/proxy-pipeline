#!/usr/bin/env bash
set -euo pipefail

# ============================================
# Setup Exit Node — VLESS+Reality + SS2022
# Настраивает протоколы через API 3X-UI
# Работает с Cherry Servers (terraform output: exit_ip)
# ============================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../.env"

# --- Определить IP Exit-ноды ---
EXIT_IP=""
if [ -f "${SCRIPT_DIR}/../terraform/terraform.tfstate" ]; then
    EXIT_IP=$(cd "${SCRIPT_DIR}/../terraform" && terraform output -raw exit_ip 2>/dev/null || echo "")
fi
if [ -z "${EXIT_IP}" ]; then
    read -rp "Введи IP Exit-ноды: " EXIT_IP
fi

PANEL_URL="http://${EXIT_IP}:${PANEL_PORT}${PANEL_PATH}"

echo "============================================"
echo "  Настройка Exit Node: ${EXIT_IP}"
echo "  Panel: ${PANEL_URL}"
echo "============================================"

# --- Ждать готовности cloud-init ---
echo "[1/8] Ожидание готовности сервера..."
for i in $(seq 1 60); do
    if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
       -i "${SSH_PRIVATE_KEY_PATH}" root@"${EXIT_IP}" \
       "test -f /opt/proxy-pipeline/.status" 2>/dev/null; then
        echo "  ✓ Сервер готов"
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "  ✗ Таймаут. Проверь: ssh root@${EXIT_IP}"
        exit 1
    fi
    echo "  Ожидание... ($i/60)"
    sleep 5
done

# --- Авторизация в 3X-UI ---
echo "[2/8] Авторизация в 3X-UI..."
COOKIES=$(mktemp)
LOGIN_RESP=$(curl -s -c "$COOKIES" \
    "${PANEL_URL}/login" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "username=${PANEL_USER}&password=${PANEL_PASS}")

if ! echo "$LOGIN_RESP" | jq -e '.success' &>/dev/null; then
    echo "  ✗ Авторизация провалилась: $LOGIN_RESP"
    rm -f "$COOKIES"
    exit 1
fi
echo "  ✓ ОК"

# --- Генерация ключей Reality ---
echo "[3/8] Генерация ключей..."
XRAY_KEYS=$(ssh -o StrictHostKeyChecking=no \
    -i "${SSH_PRIVATE_KEY_PATH}" root@"${EXIT_IP}" \
    "/usr/local/x-ui/bin/xray-linux-* x25519 2>/dev/null || xray x25519 2>/dev/null")

PRIVATE_KEY=$(echo "$XRAY_KEYS" | grep "Private" | awk '{print $NF}')
PUBLIC_KEY=$(echo "$XRAY_KEYS" | grep "Public" | awk '{print $NF}')
UUID=$(cat /proc/sys/kernel/random/uuid)
SHORT_ID=$(openssl rand -hex 4)
SS_PASSWORD=$(openssl rand -base64 16)

# Сохранить
CREDS="${SCRIPT_DIR}/../.credentials"
cat > "$CREDS" <<EOF
EXIT_IP=${EXIT_IP}
UUID=${UUID}
PRIVATE_KEY=${PRIVATE_KEY}
PUBLIC_KEY=${PUBLIC_KEY}
SHORT_ID=${SHORT_ID}
REALITY_SNI=${REALITY_SNI}
SS_PASSWORD=${SS_PASSWORD}
PANEL_URL=${PANEL_URL}
EOF
chmod 600 "$CREDS"
echo "  ✓ UUID=${UUID:0:8}... PubKey=${PUBLIC_KEY:0:12}..."

# --- Helper: добавить inbound ---
add_inbound() {
    local json="$1"
    local name="$2"
    local resp
    resp=$(curl -s -b "$COOKIES" \
        "${PANEL_URL}/panel/api/inbounds/add" \
        -H "Content-Type: application/json" \
        -d "$json")
    if echo "$resp" | jq -e '.success' &>/dev/null; then
        echo "  ✓ ${name} создан"
    else
        echo "  ✗ ${name}: $(echo "$resp" | jq -r '.msg // "error"')"
    fi
}

SNIFFING='{"enabled":true,"destOverride":["http","tls","quic","fakedns"],"metadataOnly":false,"routeOnly":false}'

# --- VLESS + Reality + XTLS-Vision ---
echo "[4/8] VLESS+Reality (порт ${REALITY_PORT})..."
add_inbound "$(jq -n \
    --arg uuid "$UUID" --arg pk "$PRIVATE_KEY" --arg sni "$REALITY_SNI" \
    --arg sid "$SHORT_ID" --arg sniff "$SNIFFING" --argjson port "${REALITY_PORT}" \
    '{
        up:0, down:0, total:0, remark:"VLESS-Reality-Vision", enable:true, expiryTime:0, listen:"",
        port: $port, protocol:"vless",
        settings: ({clients:[{id:$uuid,flow:"xtls-rprx-vision",email:"main",limitIp:0,totalGB:0,expiryTime:0,enable:true,subId:"main"}],decryption:"none",fallbacks:[]} | tostring),
        streamSettings: ({network:"tcp",security:"reality",realitySettings:{show:false,xver:0,dest:($sni+":443"),serverNames:[$sni],privateKey:$pk,shortIds:[$sid]},tcpSettings:{acceptProxyProtocol:false,header:{type:"none"}}} | tostring),
        sniffing: $sniff
    }')" "VLESS+Reality"

# --- Shadowsocks-2022 ---
echo "[5/8] Shadowsocks-2022 (порт ${SS_PORT})..."
add_inbound "$(jq -n \
    --arg method "$SS_METHOD" --arg pass "$SS_PASSWORD" --arg sniff "$SNIFFING" \
    --argjson port "${SS_PORT}" \
    '{
        up:0, down:0, total:0, remark:"SS2022-Fallback", enable:true, expiryTime:0, listen:"",
        port: $port, protocol:"shadowsocks",
        settings: ({method:$method,password:$pass,network:"tcp,udp",clients:[]} | tostring),
        streamSettings: ({"network":"tcp","security":"none","tcpSettings":{"acceptProxyProtocol":false,"header":{"type":"none"}}} | tostring),
        sniffing: $sniff
    }')" "Shadowsocks-2022"

# --- Chain порт для Bridge ---
echo "[6/8] Chain inbound (порт ${CHAIN_PORT})..."
CHAIN_UUID=$(cat /proc/sys/kernel/random/uuid)

add_inbound "$(jq -n \
    --arg uuid "$CHAIN_UUID" --arg pk "$PRIVATE_KEY" --arg sni "$REALITY_SNI" \
    --arg sid "$SHORT_ID" --arg sniff "$SNIFFING" --argjson port "${CHAIN_PORT}" \
    '{
        up:0, down:0, total:0, remark:"Chain-Bridge-Entry", enable:true, expiryTime:0, listen:"",
        port: $port, protocol:"vless",
        settings: ({clients:[{id:$uuid,flow:"",email:"bridge-relay",limitIp:0,totalGB:0,expiryTime:0,enable:true,subId:"bridge"}],decryption:"none",fallbacks:[]} | tostring),
        streamSettings: ({network:"xhttp",security:"reality",realitySettings:{show:false,xver:0,dest:($sni+":443"),serverNames:[$sni],privateKey:$pk,shortIds:[$sid]},xhttpSettings:{mode:"auto",path:"/chain-relay"}} | tostring),
        sniffing: $sniff
    }')" "Chain-xhttp"

cat >> "$CREDS" <<EOF
CHAIN_UUID=${CHAIN_UUID}
CHAIN_PORT=${CHAIN_PORT}
EOF

# --- Клиентские ссылки ---
echo "[7/8] Генерация ссылок..."
VLESS_LINK="vless://${UUID}@${EXIT_IP}:${REALITY_PORT}?encryption=none&flow=xtls-rprx-vision&security=reality&sni=${REALITY_SNI}&fp=chrome&pbk=${PUBLIC_KEY}&sid=${SHORT_ID}&type=tcp&headerType=none#Direct-VLESS-${EXIT_IP}"
SS_ENC=$(echo -n "${SS_METHOD}:${SS_PASSWORD}" | base64 -w0)
SS_LINK="ss://${SS_ENC}@${EXIT_IP}:${SS_PORT}#Direct-SS2022-${EXIT_IP}"

LINKS="${SCRIPT_DIR}/../.client-links"
cat > "$LINKS" <<EOF
# Сгенерировано $(date)
# ══════════════════════════════════════════
# ПРЯМЫЕ ССЫЛКИ (без моста, для теста)
# ══════════════════════════════════════════

${VLESS_LINK}

${SS_LINK}

# 3X-UI: ${PANEL_URL}
# Login: ${PANEL_USER} / ${PANEL_PASS}
# Subscription: http://${EXIT_IP}:${PANEL_PORT}${PANEL_PATH}/sub/main
EOF
chmod 600 "$LINKS"
echo "  ✓ Сохранены в .client-links"

# --- Проверка ---
echo "[8/8] Проверка портов..."
ALL_OK=true
for p in ${REALITY_PORT} ${SS_PORT} ${CHAIN_PORT}; do
    if ssh -o StrictHostKeyChecking=no -i "${SSH_PRIVATE_KEY_PATH}" root@"${EXIT_IP}" \
       "ss -tlnp | grep -q ':${p}'" 2>/dev/null; then
        echo "  ✓ :${p} OK"
    else
        echo "  ✗ :${p} не слушает"
        ALL_OK=false
    fi
done

rm -f "$COOKIES"

echo ""
if $ALL_OK; then
    echo "══════════════════════════════════════════"
    echo "  ✅ EXIT NODE ГОТОВ (Cherry Servers)"
    echo "══════════════════════════════════════════"
    echo "  IP:    ${EXIT_IP}"
    echo "  Panel: ${PANEL_URL}"
    echo "  Next:  bash scripts/setup-bridge.sh"
    echo "══════════════════════════════════════════"
else
    echo "  ⚠ Проблемы с портами. Логи: ssh root@${EXIT_IP} 'x-ui log'"
fi
