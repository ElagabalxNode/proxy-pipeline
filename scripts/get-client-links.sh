#!/usr/bin/env bash
set -euo pipefail

# ============================================
# Вывод клиентских ссылок + QR-коды
# ============================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LINKS_FILE="${SCRIPT_DIR}/../.client-links"
CREDS_FILE="${SCRIPT_DIR}/../.credentials"

if [ ! -f "$LINKS_FILE" ]; then
    echo "Ссылки ещё не сгенерированы. Сначала запусти:"
    echo "  bash scripts/setup-exit.sh"
    exit 1
fi

source "$CREDS_FILE"

echo "============================================"
echo "  Клиентские ссылки для подключения"
echo "============================================"
echo ""

# Показать ссылки
cat "$LINKS_FILE"

# QR-коды (если установлен qrencode)
if command -v qrencode &>/dev/null; then
    echo ""
    echo "━━━ QR-коды ━━━"

    if [ -n "${BRIDGE_UUID:-}" ]; then
        LINK="vless://${BRIDGE_UUID}@${BRIDGE_IP}:443?encryption=none&flow=xtls-rprx-vision&security=reality&sni=${REALITY_SNI}&fp=chrome&pbk=${BRIDGE_PUBLIC_KEY}&sid=${BRIDGE_SHORT_ID}&type=tcp#Bridge-VLESS"
        echo ""
        echo "VLESS через Bridge (основной):"
        qrencode -t ANSIUTF8 "$LINK"
    fi

    LINK="vless://${UUID}@${EXIT_IP}:${REALITY_PORT}?encryption=none&flow=xtls-rprx-vision&security=reality&sni=${REALITY_SNI}&fp=chrome&pbk=${PUBLIC_KEY}&sid=${SHORT_ID}&type=tcp#Direct-VLESS"
    echo ""
    echo "VLESS напрямую (запасной):"
    qrencode -t ANSIUTF8 "$LINK"
else
    echo ""
    echo "Для QR-кодов установи: apt install qrencode"
fi

echo ""
echo "━━━ Инструкция для ShadowRocket ━━━"
echo ""
echo "1. Скопируй vless:// ссылку"
echo "2. Открой ShadowRocket → '+' → Type: Import from URL"
echo "3. Вставь ссылку → Done"
echo "4. Для split-tunneling: Config → добавь URL:"
echo "   https://raw.githubusercontent.com/zeklop/shadowrocket-configs/main/template.conf"
echo ""
echo "━━━ Инструкция для Podkop (OpenWrt) ━━━"
echo ""
echo "1. LuCI → Services → Podkop"
echo "2. Вставь vless:// ссылку в поле 'Proxy URL'"
echo "3. Выбери список: 'Russia-inside'"
echo "4. Save & Apply"
echo ""
echo "━━━ Инструкция для XKeen (Keenetic) ━━━"
echo ""
echo "1. SSH на роутер (порт 222)"
echo "2. xkeen -m (выбрать Xray)"
echo "3. Вставить vless:// ссылку"
echo "4. Выбрать список: AntiZapret"
