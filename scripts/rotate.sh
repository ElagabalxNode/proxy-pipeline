#!/usr/bin/env bash
set -euo pipefail

# ============================================
# Ротация серверов (Cherry Servers)
# terraform destroy → apply → setup → bridge update
# ============================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../.env"

TARGET="${1:-}"
[ -z "$TARGET" ] && { echo "Использование: $0 <exit|bridge|all>"; exit 1; }

tg_notify() {
    [ -n "${TG_BOT_TOKEN:-}" ] && [ -n "${TG_CHAT_ID:-}" ] && \
    curl -s "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TG_CHAT_ID}" -d "text=$1" -d "parse_mode=HTML" >/dev/null 2>&1
    echo "$1"
}

rotate_exit() {
    local OLD_IP=""
    [ -f "${SCRIPT_DIR}/../.credentials" ] && OLD_IP=$(grep "EXIT_IP=" "${SCRIPT_DIR}/../.credentials" | cut -d= -f2 || true)

    tg_notify "🔄 Ротация Exit. Старый IP: ${OLD_IP:-?}"

    cd "${SCRIPT_DIR}/../terraform"

    # Удалить только сервер (SSH-ключ оставить)
    echo "[1/4] Уничтожение старого сервера..."
    terraform destroy -target=cherryservers_server.exit -auto-approve 2>/dev/null || true

    # Создать новый
    echo "[2/4] Создание нового сервера..."
    terraform apply -auto-approve
    local NEW_IP
    NEW_IP=$(terraform output -raw exit_ip)
    echo "  ✓ Новый IP: ${NEW_IP}"

    # Настроить
    echo "[3/4] Настройка 3X-UI + протоколы..."
    sleep 20
    cd "${SCRIPT_DIR}"
    bash setup-exit.sh

    # Обновить Bridge
    echo "[4/4] Перенастройка Bridge..."
    if [ -n "${BRIDGE_IP:-}" ] && [ -f "${SCRIPT_DIR}/../.credentials" ]; then
        bash setup-bridge.sh
    fi

    tg_notify "✅ Exit обновлён: ${NEW_IP}"
}

rotate_bridge() {
    echo "Bridge на RU-хостинге — ручная ротация:"
    echo "  1. Создай новый VPS"
    echo "  2. Обнови BRIDGE_IP в .env"
    echo "  3. bash scripts/setup-bridge.sh"
    echo ""
    read -rp "Новый Bridge IP (Enter = пропустить): " NEW_IP
    if [ -n "$NEW_IP" ]; then
        sed -i "s/^BRIDGE_IP=.*/BRIDGE_IP=\"${NEW_IP}\"/" "${SCRIPT_DIR}/../.env"
        tg_notify "🔄 Bridge → ${NEW_IP}"
        bash "${SCRIPT_DIR}/setup-bridge.sh"
        tg_notify "✅ Bridge обновлён: ${NEW_IP}"
    fi
}

case "$TARGET" in
    exit)   rotate_exit ;;
    bridge) rotate_bridge ;;
    all)    rotate_exit; rotate_bridge ;;
    *)      echo "Использование: $0 <exit|bridge|all>"; exit 1 ;;
esac

echo ""
echo "Готово. Ссылки: cat .client-links"
echo "Проверка: bash scripts/health-check.sh"
