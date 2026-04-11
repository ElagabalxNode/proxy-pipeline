#!/usr/bin/env bash
set -euo pipefail

# ============================================
# Авто-мониторинг (для cron)
# Проверяет доступность каждые 15 минут,
# при 3 последовательных сбоях — автоматическая ротация
#
# Установка в cron:
# */15 * * * * /path/to/proxy-pipeline/scripts/auto-monitor.sh >> /var/log/proxy-monitor.log 2>&1
# ============================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../.env"

STATE_DIR="${SCRIPT_DIR}/../.state"
mkdir -p "$STATE_DIR"

FAIL_COUNT_FILE="${STATE_DIR}/fail_count"
MAX_FAILS=3

# Получить текущий счётчик ошибок
FAIL_COUNT=0
if [ -f "$FAIL_COUNT_FILE" ]; then
    FAIL_COUNT=$(cat "$FAIL_COUNT_FILE")
fi

tg_notify() {
    local msg="$1"
    if [ -n "${TG_BOT_TOKEN:-}" ] && [ -n "${TG_CHAT_ID:-}" ]; then
        curl -s "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TG_CHAT_ID}" \
            -d "text=${msg}" \
            -d "parse_mode=HTML" > /dev/null 2>&1
    fi
}

# Быстрая проверка: TLS handshake к Exit через Bridge
check_alive() {
    if [ -f "${SCRIPT_DIR}/../.credentials" ]; then
        source "${SCRIPT_DIR}/../.credentials"
    else
        return 1
    fi

    # Проверка 1: порт Exit доступен
    if ! timeout 10 bash -c "echo >/dev/tcp/${EXIT_IP}/${REALITY_PORT}" 2>/dev/null; then
        echo "[$(date)] Exit ${EXIT_IP}:${REALITY_PORT} — порт недоступен"
        return 1
    fi

    # Проверка 2: Bridge порт (если есть мост)
    if [ -n "${BRIDGE_IP:-}" ]; then
        if ! timeout 10 bash -c "echo >/dev/tcp/${BRIDGE_IP}/443" 2>/dev/null; then
            echo "[$(date)] Bridge ${BRIDGE_IP}:443 — порт недоступен"
            return 1
        fi
    fi

    # Проверка 3: Exit может ходить в интернет
    INET_CHECK=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
        -i "${SSH_PRIVATE_KEY_PATH}" root@"${EXIT_IP}" \
        "curl -s --connect-timeout 5 -o /dev/null -w '%{http_code}' https://www.google.com" 2>/dev/null || echo "000")

    if [ "$INET_CHECK" != "200" ] && [ "$INET_CHECK" != "301" ] && [ "$INET_CHECK" != "302" ]; then
        echo "[$(date)] Exit не может достучаться до интернета (HTTP ${INET_CHECK})"
        return 1
    fi

    return 0
}

# Основная логика
if check_alive; then
    echo "[$(date)] ✓ Всё работает"
    echo "0" > "$FAIL_COUNT_FILE"
else
    FAIL_COUNT=$((FAIL_COUNT + 1))
    echo "$FAIL_COUNT" > "$FAIL_COUNT_FILE"
    echo "[$(date)] ✗ Сбой #${FAIL_COUNT}/${MAX_FAILS}"

    if [ "$FAIL_COUNT" -ge "$MAX_FAILS" ]; then
        tg_notify "🚨 ${MAX_FAILS} последовательных сбоев! Запускаю авто-ротацию Exit..."

        echo "[$(date)] Авто-ротация Exit Node..."
        bash "${SCRIPT_DIR}/rotate.sh" exit

        echo "0" > "$FAIL_COUNT_FILE"
        tg_notify "✅ Авто-ротация завершена. Проверьте .client-links"
    else
        tg_notify "⚠ Сбой #${FAIL_COUNT}/${MAX_FAILS}. Авто-ротация через $((MAX_FAILS - FAIL_COUNT)) сбоев."
    fi
fi
