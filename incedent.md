
# Инцидент 2026-10-12: потеря YC-бриджа, блокировка Exit, перенос бриджа на VDSina
Сводка событий, диагностики и восстановления. Дата: 12 октября 2026.
## 1. Исходная ситуация
- Бридж на Yandex Cloud (`130.193.37.161`, ВМ `fhm9smb969l7ip8dr1hm`) мёртв — физическая потеря ДЦ.
- Директ-подключения к Exit (`84.32.104.67`, Cherry NL) не работали.
- Оркестратор (`5.199.174.163`, Cherry LT, floating IP) «не отвечал на SSH».
## 2. Диагностика
### Оркестратор
- Сервер жив: ICMP/TCP 22 доступны, но sshd отдавал баннер с задержкой ~45 с, затем перестал отвечать совсем.
- Причина (по логам `journalctl -u ssh`): периодический **MaxStartups-троттлинг** под штурмом ботов брутфорса (`beginning MaxStartups throttling`, drop'ы с 34.52.139.129, 34.62.56.154). Ребут вернул доступ.
- Дополнительно: диск 79% (не критично), память 3.9G с ~635M available, tailscaled жив, MagicDNS работает.
- **Замечено по безопасности:** включён вход root по паролю → постоянный брутфорс → троттлинг. Требуется `PasswordAuthentication no` + ключи и/или fail2ban.
### Exit (старый, 84.32.104.67)
- Из РФ (с Mac): ICMP ✅, TCP на все порты (22/443/2053/8443) ❌ timeout.
- Shodan InternetDB видел живой sshd → сервер исправен.
- Вывод: **IP под RU-блокировкой ТСПУ (режется TCP, проходит ICMP)** — тот же феномен, что задокументирован в RUNBOOK-ROTATE-CHAIN (2026-09-11, «RU-side block of a recycled VPN range»).
### Найденные и исправленные баги в процессе
| Баг | Файл | Суть | Статус |
|---|---|---|---|
| Невалидный ключ SS2022 | `ansible/roles/exit_xui_reality/tasks/main.yml` | `openssl rand -base64 18` → 18 байт, а `2022-blake3-aes-128-gcm` требует 16 → xray crash-loop `bad key`, все инбаунды мертвы | Исправлено на `base64 16` (локально и на оркестраторе) |
| Неидемпотентность инбаундов | та же роль | submit делает только `inbounds/add` → повторный configure падает «port already used» | Workaround: ручной `inbounds/del` через API; **техдолг** — del-before-add |
| Тихая ошибка установки xray | `ansible/roles/bridge_xray_relay/tasks/main.yml` | `bash -c "$(curl -L ...)"` — при недоступном GitHub curl падает, но `set -e` не ловит ошибку подстановки → xray «не установлен», задача зелёная | **Техдолг** — переписать на `curl --fail -o file` + отдельный запуск |
| Провайдер yandex без кредов | `terraform/providers.tf` | блок `provider "yandex"` падает даже при выключенном модуле | Закомментирован (локально и на оркестраторе) |
| Автовыбор chain-режима | `orchestrator/src/pipelinectl/cli.py` | `_bridge_chain_mode()` детектит только Timeweb по tfstate; ручной бридж всегда получает `xhttp` | Добавлен override `PROXY_PIPELINE_CHAIN_MODE=ss|xhttp` (локально; на оркестраторе — по готовности) |
## 3. Восстановление
1. Мёртвый YC-бридж удалён из tfstate (`state rm` ВМ + `yandex_vpc_address`), модуль выключен (`enable_yandex_bridge = false`). Зарезервированный IP `130.193.37.161` остался выделенным в YC — отпустить вручную, чтобы не тарифицировался.
2. `pipelinectl rotate exit` → новый Exit **`88.216.61.95`** (NL-Amsterdam). Проверки с Mac: TCP 443 ✅, TLS SNI `www.nvidia.com` → настоящий сертификат NVIDIA ✅ → IP чистый для РФ.
3. После фикса SS-ключа и ручного удаления дублирующих инбаундов: `pipelinectl configure` → все порты Exit слушаются (443/8388/8443).
4. Ручной бридж на **VDSina `212.193.15.90`** (Ubuntu 22.04, root, ключ `~/.ssh/proxy_pipeline` доустановлен через `ssh-copy-id`). Xray установлен вручную (GitHub недоступен из сети VDSina — бинарь скопирован с оркестратора через scp), systemd-unit создан вручную.
5. `pipelinectl configure --bridge-only --bridge-ip 212.193.15.90 --bridge-user root` → роль отработала, identity переиспользована из бэкапа (uuid/pbk/sid прежние), ссылки пересобраны с новым IP.
## 4. Проблема egress VDSina (текущий блокер)
`validate --deep`: транспорт жив (`bridge_ssh` ✅, `tcp_bridge_443` ✅, `bridge_to_exit_chain` ✅), но все TLS-проверки с бриджа ❌.
Тесты с бриджа показали профиль ТСПУ на аплинке VDSina:
| Проверка | Результат |
|---|---|
| DNS, маршрут, ICMP, HTTP :80 | ✅ |
| TCP :443 наружу (к Exit, к зарубежным хостам) | ✅ |
| **TLS handshake до зарубежных SNI** (nvidia, microsoft, apple, github, tbank) | ❌ рвётся |
| **TLS до российских SNI** (vk.com, ya.ru, sber.ru, cdn.okko.tv) | ✅ |
Отличие от Timeweb: там DPI резал **входящий** 443-й порт бриджа (неизлечимо — плечо клиент→бридж не контролируется нами). У VDSina больны только **исходящие** TLS-плечи, которые можно перенастроить.
## 5. План доводки VDSina-бриджа (не применён, ждёт решения)
1. **Chain-плечо → SS2022** (`PROXY_PIPELINE_CHAIN_MODE=ss`): шифротекст без TLS/SNI, ТСПУ не за что зацепиться; TCP до Exit:8388 подтверждён. Инбаунд `SS2022-Fallback` на Exit готов. Требуется патч `cli.py` на оркестраторе (готов, ~6 строк + `import os`).
2. **Reality-маскировка бриджа → российский dest**: `configure --bridge-sni vk.com` — дозвон маскировки идёт к vk.com:443, что переживает ТСПУ VDSina.
3. Финал: `links --export --qr` (новые bridge-ссылки: IP 212.193.15.90, SNI vk.com) + `validate --deep` (критерий: зелёный `bridge_chain_http`).
Итоговая схема: Клиент → (VLESS+Reality, SNI=vk.com) → Бридж VDSina → (SS2022 :8388) → Exit Cherry NL → Интернет.
Риски: ss-режим ранее в проде не гонялся (теоретический статистический анализ ТСПУ); профиль аплинка VDSina может ужесточиться. Контроль — chain-probe (JSONL каждую минуту). Запасной вариант — Selectel (крупное облако, чистый egress, floating IP как у YC).
## 6. Текущее рабочее состояние
- **Директ работает**: `vless://...@88.216.61.95:443?sni=www.nvidia.com...` (см. `.client-links` на оркестраторе), можно раздавать.
- Бридж поднят, но цепочка через него — только после п.5.
- Firewall Exit (`exit_firewall`) **не применён** — роль требует IP бриджа; применится при первом полном `configure` с бриджем.
## 7. Техдолг
1. Идемпотентность `exit_xui_reality` (del-before-add по remark или update).
2. Надёжная установка xray в `bridge_xray_relay` (fail-fast + fallback-зеркало).
3. Перевод SSH оркестратора на ключи, `PasswordAuthentication no`, fail2ban.
4. Рассинхрон репозиториев: на оркестраторе новее (есть роль `exit_firewall`, которой нет в локальной копии на Mac). Синхронизировать через git; локальные правки дня: `terraform/providers.tf`, `ansible/roles/exit_xui_reality/tasks/main.yml`, `orchestrator/src/pipelinectl/cli.py` — не закоммичены.
5. Terraform-модуль `bridge_vdsina` (REST API или комьюнити-провайдер `scinfra-pro/vdsina`) — вернуть автоматическую ротацию бриджа; проверить «резервный IP» VDSina через API.
6. Резервное копирование оркестратора (tfstate, `.env`, `state/`, `.client-links`) — единая точка отказа.
7. Отпустить зарезервированный IP `130.193.37.161` в консоли YC.