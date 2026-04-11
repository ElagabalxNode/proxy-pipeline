#!/usr/bin/env bash
set -euo pipefail

# ============================================
# Setup Bridge Node (RU VPS)
# Устанавливает Xray-core как relay: Client → Bridge → Exit
# ============================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../.env"
source "${SCRIPT_DIR}/../.credentials"

echo "============================================"
echo "  Настройка Bridge Node: ${BRIDGE_IP}"
echo "  Exit Node:  ${EXIT_IP}"
echo "============================================"

SSH_CMD="ssh -o StrictHostKeyChecking=no -i ${BRIDGE_SSH_KEY} ${BRIDGE_SSH_USER}@${BRIDGE_IP} -p ${BRIDGE_SSH_PORT}"

# --- Установка Xray-core на Bridge ---
echo "[1/5] Установка Xray-core на Bridge..."
$SSH_CMD bash <<'REMOTE_INSTALL'
set -e

# BBR
cat > /etc/sysctl.d/99-bbr.conf <<EOF
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
net.ipv4.ip_forward=1
EOF
sysctl -p /etc/sysctl.d/99-bbr.conf

# Установить Xray-core (последняя версия)
bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install

# Создать директорию конфигов
mkdir -p /usr/local/etc/xray
echo "  Xray установлен: $(xray version | head -1)"
REMOTE_INSTALL

echo "  ✓ Xray-core установлен"

# --- Генерация ключей для Bridge ---
echo "[2/5] Генерация ключей Bridge Reality..."

BRIDGE_KEYS=$($SSH_CMD "xray x25519")
BRIDGE_PRIVATE_KEY=$(echo "$BRIDGE_KEYS" | grep "Private" | awk '{print $NF}')
BRIDGE_PUBLIC_KEY=$(echo "$BRIDGE_KEYS" | grep "Public" | awk '{print $NF}')
BRIDGE_UUID=$(cat /proc/sys/kernel/random/uuid)
BRIDGE_SHORT_ID=$(openssl rand -hex 4)

echo "  ✓ Bridge UUID: ${BRIDGE_UUID}"
echo "  ✓ Bridge Public Key: ${BRIDGE_PUBLIC_KEY}"

# Сохранить
cat >> "${SCRIPT_DIR}/../.credentials" <<EOF
BRIDGE_UUID=${BRIDGE_UUID}
BRIDGE_PRIVATE_KEY=${BRIDGE_PRIVATE_KEY}
BRIDGE_PUBLIC_KEY=${BRIDGE_PUBLIC_KEY}
BRIDGE_SHORT_ID=${BRIDGE_SHORT_ID}
EOF

# --- Создать конфиг Xray на Bridge ---
echo "[3/5] Загрузка конфигурации Xray..."

XRAY_CONFIG=$(cat <<ENDJSON
{
    "log": {
        "loglevel": "warning",
        "access": "/var/log/xray/access.log",
        "error": "/var/log/xray/error.log"
    },
    "dns": {
        "servers": [
            "https+local://1.1.1.1/dns-query",
            "localhost"
        ]
    },
    "inbounds": [
        {
            "tag": "vless-reality-in",
            "listen": "0.0.0.0",
            "port": 443,
            "protocol": "vless",
            "settings": {
                "clients": [
                    {
                        "id": "${BRIDGE_UUID}",
                        "flow": "xtls-rprx-vision",
                        "email": "client-via-bridge"
                    }
                ],
                "decryption": "none"
            },
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "show": false,
                    "dest": "${REALITY_SNI}:443",
                    "xver": 0,
                    "serverNames": ["${REALITY_SNI}"],
                    "privateKey": "${BRIDGE_PRIVATE_KEY}",
                    "shortIds": ["${BRIDGE_SHORT_ID}"]
                }
            },
            "sniffing": {
                "enabled": true,
                "destOverride": ["http", "tls", "quic"]
            }
        },
        {
            "tag": "ss2022-in",
            "listen": "0.0.0.0",
            "port": ${SS_PORT},
            "protocol": "shadowsocks",
            "settings": {
                "method": "${SS_METHOD}",
                "password": "${SS_PASSWORD}",
                "network": "tcp,udp"
            },
            "sniffing": {
                "enabled": true,
                "destOverride": ["http", "tls", "quic"]
            }
        }
    ],
    "outbounds": [
        {
            "tag": "chain-to-exit",
            "protocol": "vless",
            "settings": {
                "vnext": [
                    {
                        "address": "${EXIT_IP}",
                        "port": ${CHAIN_PORT},
                        "users": [
                            {
                                "id": "${CHAIN_UUID}",
                                "encryption": "none",
                                "flow": ""
                            }
                        ]
                    }
                ]
            },
            "streamSettings": {
                "network": "xhttp",
                "security": "reality",
                "realitySettings": {
                    "show": false,
                    "fingerprint": "chrome",
                    "serverName": "${REALITY_SNI}",
                    "publicKey": "${PUBLIC_KEY}",
                    "shortId": "${SHORT_ID}",
                    "spiderX": ""
                },
                "xhttpSettings": {
                    "mode": "auto",
                    "path": "/chain-relay"
                }
            }
        },
        {
            "tag": "direct",
            "protocol": "freedom",
            "settings": {}
        },
        {
            "tag": "block",
            "protocol": "blackhole",
            "settings": {}
        }
    ],
    "routing": {
        "domainStrategy": "AsIs",
        "rules": [
            {
                "type": "field",
                "outboundTag": "block",
                "protocol": ["bittorrent"]
            },
            {
                "type": "field",
                "outboundTag": "direct",
                "ip": ["geoip:private"]
            },
            {
                "type": "field",
                "outboundTag": "chain-to-exit",
                "port": "0-65535"
            }
        ]
    }
}
ENDJSON
)

echo "$XRAY_CONFIG" | $SSH_CMD "cat > /usr/local/etc/xray/config.json"

echo "  ✓ Конфиг загружен"

# --- Создать лог-директорию и запустить ---
echo "[4/5] Запуск Xray на Bridge..."
$SSH_CMD bash <<'REMOTE_START'
mkdir -p /var/log/xray
systemctl enable xray
systemctl restart xray
sleep 3
if systemctl is-active --quiet xray; then
    echo "OK"
else
    echo "FAIL"
    journalctl -u xray --no-pager -n 20
fi
REMOTE_START

echo "  ✓ Xray запущен"

# --- Генерация Bridge-ссылок для клиентов ---
echo "[5/5] Генерация клиентских ссылок через Bridge..."

BRIDGE_VLESS_LINK="vless://${BRIDGE_UUID}@${BRIDGE_IP}:443?encryption=none&flow=xtls-rprx-vision&security=reality&sni=${REALITY_SNI}&fp=chrome&pbk=${BRIDGE_PUBLIC_KEY}&sid=${BRIDGE_SHORT_ID}&type=tcp&headerType=none#Bridge-VLESS-${BRIDGE_IP}"

BRIDGE_SS_ENCODED=$(echo -n "${SS_METHOD}:${SS_PASSWORD}" | base64 -w0)
BRIDGE_SS_LINK="ss://${BRIDGE_SS_ENCODED}@${BRIDGE_IP}:${SS_PORT}#Bridge-SS2022-${BRIDGE_IP}"

# Обновить файл ссылок
cat >> "${SCRIPT_DIR}/../.client-links" <<EOF

# ============================================
# ЧЕРЕЗ МОСТ (Bridge) — рекомендуется!
# ============================================

# --- VLESS + Reality через Bridge (основной) ---
${BRIDGE_VLESS_LINK}

# --- Shadowsocks-2022 через Bridge (запасной) ---
${BRIDGE_SS_LINK}

EOF

echo ""
echo "============================================"
echo "  ✓ BRIDGE NODE ГОТОВ"
echo "============================================"
echo "  Bridge: ${BRIDGE_IP}"
echo "  Exit:   ${EXIT_IP}"
echo "  Chain:  Client → Bridge(443) → Exit(${CHAIN_PORT}) → Internet"
echo ""
echo "  ★ Используй Bridge-ссылки (рекомендуется):"
echo "  ${BRIDGE_VLESS_LINK}"
echo ""
echo "  Проверка: bash health-check.sh"
echo "============================================"
