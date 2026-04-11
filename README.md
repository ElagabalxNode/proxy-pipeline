# Proxy Pipeline — Автоматическое развёртывание и ротация

## Архитектура

```
[Клиент] ──VLESS+Reality──▶ [Bridge RU VPS] ──xhttp──▶ [Exit EU VPS] ──▶ [Интернет]
   │                              │                          │
   │  ShadowRocket / Podkop      │  Yandex Cloud /          │  Aeza / Fornex /
   │  NekoBox / Hiddify          │  VK Cloud / FirstByte    │  Oracle Free
   │                              │                          │
   └──────────────────────────────┴──────────────────────────┘
                                  │
                          [3X-UI панель управления]
                          (на Exit-ноде, порт 2053)
```

## Что делает пайплайн

1. **Terraform** создаёт Exit VPS (Hetzner/Aeza API) и Bridge VPS
2. **Cloud-init** автоматически ставит Docker, 3X-UI, настраивает BBR
3. **setup-exit.sh** конфигурирует VLESS+Reality + Shadowsocks-2022 через API 3X-UI
4. **setup-bridge.sh** настраивает Xray-relay на мосту (RU → EU)
5. **health-check.sh** проверяет связность и алертит в Telegram
6. **rotate.sh** уничтожает заблокированный сервер и создаёт новый за 3 минуты

## Быстрый старт

```bash
# 1. Заполни переменные
cp .env.example .env
nano .env

# 2. Разверни Exit-ноду
cd terraform && terraform init && terraform apply -auto-approve

# 3. Подожди 2-3 минуты пока cloud-init отработает, затем настрой
cd ../scripts && bash setup-exit.sh

# 4. Разверни и настрой Bridge
bash setup-bridge.sh

# 5. Проверь всё
bash health-check.sh

# 6. Получи ссылки для клиентов
bash get-client-links.sh
```

## Ротация при блокировке

```bash
# Ротация Exit-ноды (новый IP за 3 минуты)
bash scripts/rotate.sh exit

# Ротация Bridge (если заблокировали)
bash scripts/rotate.sh bridge

# Полная ротация обоих
bash scripts/rotate.sh all
```

## Требования

- Аккаунт Cherry Servers (portal.cherryservers.com) + API token
  Оплата: крипто (BTC, ETH, USDC, SOL...) или карта
- Аккаунт для Bridge VPS (Timeweb / Yandex Cloud / любой RU-хостинг)
- Terraform >= 1.5
- jq, curl, ssh-keygen
- Telegram Bot Token (опционально, для алертов)

## Регионы Cherry Servers

| Slug | Город | Для чего |
|------|-------|----------|
| EU-Nord-1 | Stockholm | ★ Рекомендован для Exit |
| NL-Ams-1 | Amsterdam | Альтернатива Exit |
| DE-Fra-1 | Frankfurt | Альтернатива Exit |
| EU-East-1 | Lithuania | Запасной Exit |
