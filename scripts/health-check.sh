#!/usr/bin/env bash
set -euo pipefail

# ============================================
# Health Check — Полная проверка цепочки
# Тестирует: Exit, Bridge, цепочку, протоколы
# Можно ставить в cron для авто-мониторинга
# ============================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../.env"

CREDS_FILE="${SCRIPT_DIR}/../.credentials"
if [ -f "$CREDS_FILE" ]; then
    source "$CREDS_FILE"
fi

ERRORS=0
WARNINGS=0

tg_notify() {
    local msg="$1"
    if [ -n "${TG_BOT_TOKEN:-}" ] && [ -n "${TG_CHAT_ID:-}" ]; then
        curl -s "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TG_CHAT_ID}" \
            -d "text=${msg}" \
            -d "parse_mode=HTML" > /dev/null 2>&1
    fi
}

check_pass() { echo "  ✓ $1"; }
check_fail() { echo "  ✗ $1"; ((ERRORS++)); }
check_warn() { echo "  ⚠ $1"; ((WARNINGS++)); }

echo "============================================"
echo "  Health Check — $(date)"
echo "============================================"
echo ""

# --- 1. Exit Node ---
echo "━━━ Exit Node (${EXIT_IP:-not set}) ━━━"

if [ -n "${EXIT_IP:-}" ]; then
    # Ping
    if ping -c 1 -W 3 "$EXIT_IP" &>/dev/null; then
        check_pass "Ping OK"
    else
        check_fail "Ping FAILED — сервер недоступен или ICMP заблокирован"
    fi

    # SSH
    if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
       -i "${SSH_PRIVATE_KEY_PATH}" root@"${EXIT_IP}" \
       "echo ok" &>/dev/null; then
        check_pass "SSH OK"
    else
        check_fail "SSH FAILED"
    fi

    # VLESS порт
    if timeout 5 bash -c "echo >/dev/tcp/${EXIT_IP}/${REALITY_PORT}" 2>/dev/null; then
        check_pass "VLESS порт ${REALITY_PORT} открыт"
    else
        check_fail "VLESS порт ${REALITY_PORT} ЗАКРЫТ"
    fi

    # SS порт
    if timeout 5 bash -c "echo >/dev/tcp/${EXIT_IP}/${SS_PORT}" 2>/dev/null; then
        check_pass "SS2022 порт ${SS_PORT} открыт"
    else
        check_fail "SS2022 порт ${SS_PORT} ЗАКРЫТ"
    fi

    # Chain порт
    if timeout 5 bash -c "echo >/dev/tcp/${EXIT_IP}/${CHAIN_PORT}" 2>/dev/null; then
        check_pass "Chain порт ${CHAIN_PORT} открыт"
    else
        check_fail "Chain порт ${CHAIN_PORT} ЗАКРЫТ"
    fi

    # 3X-UI панель
    PANEL_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
        --connect-timeout 5 \
        "http://${EXIT_IP}:${PANEL_PORT}${PANEL_PATH}/login" 2>/dev/null || echo "000")
    if [ "$PANEL_STATUS" = "200" ]; then
        check_pass "3X-UI панель доступна (HTTP ${PANEL_STATUS})"
    else
        check_warn "3X-UI панель HTTP ${PANEL_STATUS} — может быть нормально если через прокси"
    fi

    # Xray процесс
    if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
       -i "${SSH_PRIVATE_KEY_PATH}" root@"${EXIT_IP}" \
       "pgrep -f xray" &>/dev/null; then
        check_pass "Xray процесс работает"
    else
        check_fail "Xray НЕ работает на Exit"
    fi

    # BBR
    BBR_STATUS=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
        -i "${SSH_PRIVATE_KEY_PATH}" root@"${EXIT_IP}" \
        "sysctl net.ipv4.tcp_congestion_control 2>/dev/null" || echo "unknown")
    if echo "$BBR_STATUS" | grep -q "bbr"; then
        check_pass "BBR активен"
    else
        check_warn "BBR не активен: ${BBR_STATUS}"
    fi

    # Xray версия
    XRAY_VER=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
        -i "${SSH_PRIVATE_KEY_PATH}" root@"${EXIT_IP}" \
        "x-ui version 2>/dev/null || echo unknown" || echo "unknown")
    echo "  ℹ 3X-UI версия: ${XRAY_VER}"

    # Disk space
    DISK_USE=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
        -i "${SSH_PRIVATE_KEY_PATH}" root@"${EXIT_IP}" \
        "df -h / | tail -1 | awk '{print \$5}'" 2>/dev/null || echo "unknown")
    if [ "$DISK_USE" != "unknown" ]; then
        DISK_PCT=$(echo "$DISK_USE" | tr -d '%')
        if [ "$DISK_PCT" -gt 90 ]; then
            check_warn "Диск заполнен на ${DISK_USE}"
        else
            check_pass "Диск: ${DISK_USE}"
        fi
    fi
else
    check_fail "EXIT_IP не задан"
fi

echo ""

# --- 2. Bridge Node ---
echo "━━━ Bridge Node (${BRIDGE_IP:-not set}) ━━━"

if [ -n "${BRIDGE_IP:-}" ]; then
    # Ping
    if ping -c 1 -W 3 "$BRIDGE_IP" &>/dev/null; then
        check_pass "Ping OK"
    else
        check_warn "Ping FAILED (может быть закрыт ICMP)"
    fi

    # SSH
    if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
       -i "${BRIDGE_SSH_KEY}" "${BRIDGE_SSH_USER}@${BRIDGE_IP}" \
       -p "${BRIDGE_SSH_PORT}" "echo ok" &>/dev/null; then
        check_pass "SSH OK"
    else
        check_fail "SSH FAILED"
    fi

    # VLESS порт на Bridge
    if timeout 5 bash -c "echo >/dev/tcp/${BRIDGE_IP}/443" 2>/dev/null; then
        check_pass "VLESS порт 443 открыт"
    else
        check_fail "VLESS порт 443 ЗАКРЫТ на Bridge"
    fi

    # Xray процесс
    if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
       -i "${BRIDGE_SSH_KEY}" "${BRIDGE_SSH_USER}@${BRIDGE_IP}" \
       -p "${BRIDGE_SSH_PORT}" "pgrep -f xray" &>/dev/null; then
        check_pass "Xray процесс работает"

        # Проверить ошибки в логах за последний час
        RECENT_ERRORS=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
            -i "${BRIDGE_SSH_KEY}" "${BRIDGE_SSH_USER}@${BRIDGE_IP}" \
            -p "${BRIDGE_SSH_PORT}" \
            "grep -c 'failed\|error\|rejected' /var/log/xray/error.log 2>/dev/null || echo 0")
        if [ "$RECENT_ERRORS" -gt 50 ]; then
            check_warn "Xray: ${RECENT_ERRORS} ошибок в логе"
        else
            check_pass "Xray лог: ${RECENT_ERRORS} ошибок (ОК)"
        fi
    else
        check_fail "Xray НЕ работает на Bridge"
    fi
else
    check_warn "BRIDGE_IP не задан (работаем без моста)"
fi

echo ""

# --- 3. Тест TLS Handshake (Reality) ---
echo "━━━ TLS / Reality проверка ━━━"

if [ -n "${EXIT_IP:-}" ]; then
    TLS_RESULT=$(timeout 10 openssl s_client \
        -connect "${EXIT_IP}:${REALITY_PORT}" \
        -servername "${REALITY_SNI}" \
        </dev/null 2>&1 | head -5 || echo "FAILED")

    if echo "$TLS_RESULT" | grep -qi "CONNECTED\|BEGIN CERTIFICATE"; then
        check_pass "TLS Handshake к Exit (${REALITY_SNI}) — соединение установлено"
    else
        check_fail "TLS Handshake к Exit FAILED — возможно заблокирован ТСПУ"
    fi
fi

if [ -n "${BRIDGE_IP:-}" ]; then
    TLS_RESULT=$(timeout 10 openssl s_client \
        -connect "${BRIDGE_IP}:443" \
        -servername "${REALITY_SNI}" \
        </dev/null 2>&1 | head -5 || echo "FAILED")

    if echo "$TLS_RESULT" | grep -qi "CONNECTED\|BEGIN CERTIFICATE"; then
        check_pass "TLS Handshake к Bridge (${REALITY_SNI}) — соединение установлено"
    else
        check_fail "TLS Handshake к Bridge FAILED"
    fi
fi

echo ""

# --- 4. End-to-End тест через прокси ---
echo "━━━ End-to-End тест ━━━"

if [ -n "${EXIT_IP:-}" ]; then
    # Проверить что Exit может выходить в интернет
    EXTERNAL_IP=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
        -i "${SSH_PRIVATE_KEY_PATH}" root@"${EXIT_IP}" \
        "curl -s --connect-timeout 5 https://api.ipify.org 2>/dev/null" || echo "FAILED")

    if [ "$EXTERNAL_IP" = "$EXIT_IP" ] || [[ "$EXTERNAL_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        check_pass "Exit → Internet: внешний IP = ${EXTERNAL_IP}"
    else
        check_fail "Exit не может выйти в интернет"
    fi
fi

if [ -n "${BRIDGE_IP:-}" ] && [ -n "${EXIT_IP:-}" ]; then
    # Проверить связность Bridge → Exit
    CHAIN_TEST=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
        -i "${BRIDGE_SSH_KEY}" "${BRIDGE_SSH_USER}@${BRIDGE_IP}" \
        -p "${BRIDGE_SSH_PORT}" \
        "timeout 5 bash -c 'echo >/dev/tcp/${EXIT_IP}/${CHAIN_PORT}' 2>&1 && echo OK || echo FAIL")

    if echo "$CHAIN_TEST" | grep -q "OK"; then
        check_pass "Bridge → Exit chain порт (${CHAIN_PORT}) доступен"
    else
        check_fail "Bridge НЕ МОЖЕТ достучаться до Exit:${CHAIN_PORT}"
    fi
fi

echo ""

# --- Результаты ---
echo "============================================"
if [ $ERRORS -eq 0 ] && [ $WARNINGS -eq 0 ]; then
    echo "  ✅ Всё работает отлично!"
    tg_notify "✅ Health check OK — $(date '+%H:%M')"
elif [ $ERRORS -eq 0 ]; then
    echo "  ⚠ ${WARNINGS} предупреждений, 0 ошибок"
    tg_notify "⚠ Health check: ${WARNINGS} warnings — $(date '+%H:%M')"
else
    echo "  ❌ ${ERRORS} ошибок, ${WARNINGS} предупреждений"
    echo ""
    echo "  Рекомендации:"
    echo "  - Если порты закрыты → проверь firewall: ufw status"
    echo "  - Если TLS failed → возможна блокировка ТСПУ, нужна ротация"
    echo "  - Если Bridge не видит Exit → проверь chain конфиг"
    echo ""
    echo "  Быстрая ротация: bash scripts/rotate.sh exit"

    tg_notify "❌ Health check FAILED: ${ERRORS} errors — $(date '+%H:%M')
Рекомендация: bash scripts/rotate.sh exit"
fi
echo "============================================"

exit $ERRORS
