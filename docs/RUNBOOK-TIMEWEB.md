# Ранбук: миграция Bridge на Timeweb Cloud

Перенос RU-бриджа с Yandex Cloud (~3500 ₽/мес) на Timeweb Cloud (~630 ₽/мес).
Экономия ~75%. Бесшовное переключение (без простоя).

> Документ разделён по **зонам ответственности**: 🧑 — делаешь ты руками (панель
> Timeweb / Yandex / твой терминал), 🤖 — сделано в коде (ты ревьюишь и мёржишь).

---

## Текущий статус (live, 2026-06-23)

| Шаг | Статус | Факт |
|---|---|---|
| Токен Timeweb + `.env` | ✅ | `TWC_TOKEN` в `.env`, `source .env` экспортирует канонические имена |
| `tfvars` (оба бриджа вкл.) | ✅ | `enable_timeweb_bridge=true` (ru-2/msk-1), `enable_yandex_bridge=true` |
| `terraform init` | ✅ | провайдер `timeweb-cloud` v1.7.1 |
| `apply` Timeweb-сервер | ✅ | `twc_server` id=`8444247`, `twc_ssh_key` id=`707085`; **msk-1 принят** (зонный риск снят) |
| Публичный IPv4 | ✅ | сервер пришёл **IPv6-only** → заказан `twc_server_ip` = **`<TW_BRIDGE_IP>`** |
| SSH до моста | ✅ | root по ключу работает, Ubuntu 22.04.5 |
| `configure --bridge-only` | ⏳ след. шаг | `--bridge-ip <TW_BRIDGE_IP> --bridge-user root` |
| `validate --deep` / `links` | ⏳ | — |
| Погасить Yandex (шаг 9) | ⏳ | только после зелёного `validate` и переезда клиентов |

> 💸 На первом `apply` была ошибка `not enough money on account balance` — пополнили
> баланс Timeweb, повторили. Учитывай: сервер + IPv4 ≈ 630 ₽/мес, держи баланс с запасом.

---

## Зафиксированные решения

| Параметр | Значение |
|---|---|
| Локация | Москва — `availability_zone = msk-1`, `configurator_location = ru-2` (msk-1 подтверждён вживую) |
| Тариф | Cloud-15: 1 vCPU / 1 GB / 15 GB NVMe (≈630 ₽/мес с IP) |
| Публичный IP | IPv4 отдельным `twc_server_ip` (Timeweb отдаёт сервер IPv6-only); умирает вместе с сервером при rotate |
| Cutover | без простоя: оба бриджа параллельно, потом гасим Yandex |
| Провайдер | `timeweb-cloud/timeweb-cloud` `~> 1.7` (стоит 1.7.1) |

---

## Трогаем ли мы Exit? (коротко: нет)

- **Terraform — НЕТ.** Все apply таргетированы (`-target=module.bridge_timeweb` / `module.bridge`). Exit (`cherryservers_server.exit`) не входит в граф зависимостей цели → не рефрешится и не меняется.
- **Ansible — НЕТ при `configure --bridge-only`.** Это запускает `site.yml --limit bridge`: play «Configure Exit node» получает 0 хостов и пропускается. Бридж берёт ВСЕ параметры Exit из `state/runtime.json`, по SSH к Exit не ходит.
- ⚠️ **ТРОГАЮТ Exit (на миграции НЕ используем):** обычный `pipelinectl configure` (без флага — гоняет роль Exit, она не идемпотентна) и `pipelinectl provision` (полный `terraform apply` — может захотеть пересоздать Exit, см. предупреждение ниже).
- Из `state/` при bridge-only меняется только `bridge_runtime.json` (ключи/IP нового бриджа). `runtime.json` (Exit) не трогается.

## Что при двух бриджах одновременно?

- Две независимые VM: YC (старая) и Timeweb (новая). Обе релеят на **один** Exit:`chain_port`, используя одни и те же chain-настройки Exit из `runtime.json` (общий `chain_uuid` — для VLESS несколько соединений с одним id это норма).
- ⚠️ **Про outputs:** при `-target` apply корневые `terraform output` НЕ обновляются (Terraform предупреждает об этом) — `bridge_ip` остаётся от YC. Поэтому `configure --bridge-only` мы запускаем с **явным `--bridge-ip`** Timeweb-моста, а не полагаемся на output. (Приоритет Timeweb в outputs `try(bridge_timeweb, bridge, "")` срабатывает только после ПОЛНОГО `apply` — это важно для будущего `rotate`.)
- `pipelinectl links` соберёт ссылки на Timeweb (из свежего `bridge_runtime.json`). **Старые** клиенты идут через YC, пока он жив; **новые** — через Timeweb. Оба маршрута рабочие одновременно → ноль простоя. Платишь за оба, пока не погасишь YC (шаг 9).
- Условие бесшовности: не менять chain-настройки Exit (мы их и не трогаем).

> ⚠️ **Замеченное расхождение:** `terraform output bridge_ip` = `<BRIDGE_TF_IP>` (так Terraform видит YC-VM), а `state/bridge_runtime.json` = `<BRIDGE_LIVE_IP>` (на этот IP смотрят уже выданные клиентам ссылки). Живой YC-бридж и конфиг клиентов рассинхронены (вероятно, YC-VM пересоздавали без `configure`). Гарантия «ноль простоя для УЖЕ существующих клиентов» поэтому не железная — проверь, через какой IP клиенты реально ходят сейчас. Для НОВЫХ (Timeweb) ссылок всё чисто.

## Что уже сделано в коде (🤖 моя зона)

- Модуль `terraform/modules/bridge_timeweb/`: `twc_ssh_key` + `twc_server` (size через `configuration{cpu,ram,disk}` в МБ) + **`twc_server_ip` (IPv4, т.к. Timeweb даёт сервер IPv6-only)**; output `external_ip` = `twc_server_ip.bridge.ip`.
- Корневой стек: провайдер `twc` в `versions.tf`/`providers.tf`; переменные `enable_timeweb_bridge` + `timeweb_*` в `variables.tf`; модуль в `main.tf`; `outputs.tf` отдаёт IP активного бриджа (Timeweb приоритетнее Yandex).
- `pipelinectl rotate` выбирает цель из состояния Terraform — работает и с Timeweb (`twc_server.bridge`), и с Yandex, без правок кода под провайдера.
- `pipelinectl configure --bridge-only [--bridge-ip --bridge-user]`: настраивает ТОЛЬКО бридж (`site.yml --limit bridge`), Exit не трогает; override IP/user обходит устаревшие после `-target` outputs. `rotate bridge` теперь тоже bridge-only (раньше зря гонял роль Exit).
- `terraform.tfvars.example` (его не было — чинит шаг setup в README).
- README / `.env.example` / `config/pipeline.example.yaml` обновлены; `.env` сделан source-абельным (канонические имена); убран баг с backtick, ронявший конфиг и CI.
- **Починен предсуществующий баг** в `modules/exit_cherry/main.tf`: heredoc `user_data` не экранирован → весь стек не проходил `validate`. Теперь `terraform validate` зелёный.

> ⚠️ **Важно про живой Exit.** Из-за фикса heredoc отрендеренный `user_data` мог
> разойтись с тем, что записано в state при первом деплое Exit. Поэтому **полный**
> `terraform apply` (или `pipelinectl provision`) может захотеть **пересоздать Exit**
> (`cherryservers_server.exit`). Чтобы этого не случилось при миграции, все apply
> **таргетированы только на бридж**. Если в плане увидишь `cherryservers_server.exit`
> с пометкой replace — останови apply и сообщи мне (закроем риск навсегда через
> `lifecycle { ignore_changes = [user_data] }`; `user_data` нужен только при первой загрузке).

Роль `bridge_xray_relay` и команды `validate`/`links` от провайдера не зависят — читают `state/runtime.json` + `state/bridge_runtime.json`.

---

## Предусловия (🧑 проверь перед стартом)

- Exit поднят и настроен (`state/runtime.json` есть, `terraform output exit_ip` непустой). Сейчас Exit = `<EXIT_IP>`.
- Есть `~/.ssh/proxy_pipeline` и `~/.ssh/proxy_pipeline.pub`.
- Установлен `pipelinectl` (editable — правки кода применяются сразу).
- На балансе Timeweb есть деньги (сервер + IPv4 ≈ 630 ₽/мес).

---

## Шаг 1 (🧑). Создать API-токен Timeweb

1. https://timeweb.cloud/ → правый верхний угол → **«Настройки аккаунта»**.
2. **«API и Terraform»** — прямая ссылка: **https://timeweb.cloud/my/api-keys**.
3. **«Добавить токен»**, заполнить:
   - **Имя**: `terraform-proxy-pipeline`.
   - **Срок действия**: на выбор («Бессрочно» удобно, фикс-срок безопаснее).
   - ✅ **«Удалять сервисы по API без кода в Телеграме»** — **ВКЛЮЧИТЬ** (иначе `destroy`/`rotate` зависнут).
   - **«Ограниченные права»**: можно ограничить сервисом **«Облачные серверы»**.
4. **«Выпустить»** → **сразу скопировать** токен (JWT `eyJ...`, повторно не показывается).

## Шаг 2 (🧑). Проверить подтверждение удаления

«Настройки аккаунта» → «Уведомления» → «Безопасность» → «Подтверждение удаления сервисов».
Достаточно флага токена из шага 1 (защита панели остаётся). На весь аккаунт отключать не нужно.

## Шаг 3 (🧑). Бэкап состояния и секреты в окружение

```bash
cd /root/proxy-pipeline

# 0) Бэкап стейта
cp terraform/terraform.tfstate "terraform/terraform.tfstate.pre-timeweb.$(date +%Y%m%d-%H%M%S).bak"

# 1) Впиши токен Timeweb в .env — строку:  export TWC_TOKEN="eyJ..."
#    (cherry/panel-секреты там уже есть). Затем:
source .env

# 2) Проверка (значения не печатаем):
[ -n "$TF_VAR_cherry_api_token" ] && [ -n "$TWC_TOKEN" ] && echo "env OK" || echo "ЗАПОЛНИ .env (cherry/panel/TWC)"
```

> `.env` сам экспортирует канонические имена (блок внизу файла). Terraform читает
> `TF_VAR_cherry_api_token` и `TF_VAR_panel_pass` (без дефолтов — без них падает даже
> таргетированный `apply`); `cherry_project_id`, `exit_region` и пр. — из `terraform.tfvars`.

## Шаг 4 (🧑). Включить Timeweb, оставить Yandex (для бесшовности)

**ДОБАВЬ** в `terraform/terraform.tfvars` блок Timeweb (одним местом):

```hcl
enable_timeweb_bridge         = true
timeweb_configurator_location = "ru-2"
timeweb_availability_zone     = "msk-1"   # "" если plan/apply отвергнет зону
timeweb_cpu                   = 1
timeweb_ram_mb                = 1024
timeweb_disk_mb               = 15360
```

`enable_yandex_bridge = true` **уже есть** в секции Yandex — оставь его `true` на время cutover и **НЕ дублируй** (иначе `Error: Attribute redefined`).

## Шаг 5 (🧑). Init / plan / apply (только бридж)

```bash
cd /root/proxy-pipeline/terraform
terraform init                                  # провайдер timeweb-cloud (v1.7.x)
terraform plan  -target=module.bridge_timeweb   # ОЖИДАЕМ: + twc_ssh_key + twc_server + twc_server_ip (3 to add); Exit и YC не трогаются
terraform apply -target=module.bridge_timeweb
```

- **`-target=module.bridge_timeweb`** намеренно: создаём только бридж (сервер + ключ + IPv4), не трогая Exit и YC. Не используй `pipelinectl provision` (полный apply — см. предупреждение про Exit).
- Если plan ругается на зону → `timeweb_availability_zone = ""` (или регион `ru-1`) и повтори.
- Если `Error: not enough money on account balance` → **пополни баланс Timeweb** и повтори `apply` (созданный ранее `twc_ssh_key` не пересоздаётся).
- ⚠️ Корневой `terraform output bridge_ip` после `-target` **останется от YC** — это нормально, IP моста берём из ресурса (шаг 6), а не из output.

## Шаг 6 (🧑). Узнать IPv4 моста и настроить его

Timeweb поднимает сервер **IPv6-only**; модуль заказывает IPv4 (`twc_server_ip`), он известен после `apply`:

```bash
cd /root/proxy-pipeline/terraform
terraform state show 'module.bridge_timeweb[0].twc_server_ip.bridge' | grep -E '^[[:space:]]+ip[[:space:]]+='
# IP виден и в панели Timeweb. Если ресурса нет — добей: terraform apply -target=module.bridge_timeweb
```

(необязательно) проверить доступность моста по SSH до запуска ansible:
```bash
ssh -i ~/.ssh/proxy_pipeline -o StrictHostKeyChecking=accept-new root@<TIMEWEB_IP> 'echo SSH_OK; uname -a'
```

Настроить ТОЛЬКО бридж:
```bash
cd /root/proxy-pipeline
pipelinectl configure --bridge-only --bridge-ip <TIMEWEB_IP> --bridge-user root
```

> `--bridge-only` → `site.yml --limit bridge`: play для Exit получает 0 хостов → Exit не трогается. `--bridge-ip`/`--bridge-user` обходят устаревшие после `-target` outputs. Роль ставит xray, генерит ключи моста, поднимает VLESS+Reality:443 с outbound-цепочкой на Exit и пишет `state/bridge_runtime.json`.

(текущий прогон: `--bridge-ip <TW_BRIDGE_IP> --bridge-user root`)

## Шаг 7 (🧑). Проверить цепочку

```bash
pipelinectl validate --deep   # SSH, порты 443/8388, цепочка Bridge→Exit, внешний IP = IP Exit
```
`validate` берёт `bridge_ip` из `state/bridge_runtime.json` (свежий Timeweb), не из terraform output.

## Шаг 8 (🧑). Перевыпустить и раздать ссылки

```bash
pipelinectl links --export --qr   # IP и ключи моста сменились — клиентам нужны новые ссылки/QR
```
Раздать клиентам, дождаться, что ключевые клиенты переключились на Timeweb.

## Шаг 9 (🧑). Погасить Yandex (остановить биллинг) — только после зелёного шага 7 и переезда клиентов

```hcl
# terraform/terraform.tfvars
enable_yandex_bridge = false
```

```bash
cd /root/proxy-pipeline/terraform
export YC_TOKEN="..."                      # нужен для уничтожения YC-ресурсов
terraform plan  -target=module.bridge      # ОЖИДАЕМ: только - yandex_compute_instance.bridge; Exit/Timeweb не трогаются
terraform apply -target=module.bridge
```

Проверить в **консоли Yandex Cloud**, что VM, диск и публичный IP удалены и не тарифицируются.
(После этого по желанию — удалить YC-модуль/провайдер из кода; скажи, оформлю отдельным PR.)

---

## Эксплуатация: ротация бриджа

Если Timeweb-IP попадёт под блокировку — пересоздать мост с новым IP:

```bash
pipelinectl rotate bridge         # уничтожит twc_server.bridge (+IPv4), создаст заново, повторит configure (bridge-only)
pipelinectl links --export --qr    # раздать новые ссылки
```

`rotate bridge` сам находит активный мост в состоянии Terraform (Timeweb приоритетен), делает полный `apply` (outputs пересчитываются корректно) и configure только бриджа. Новый `twc_server_ip` = новый IPv4.

---

## Откат (если Timeweb не подходит)

```hcl
# terraform.tfvars
enable_timeweb_bridge = false
enable_yandex_bridge  = true
```

```bash
export YC_TOKEN="..."   # + при необходимости YC_CLOUD_ID / YC_FOLDER_ID, yandex_folder_id/subnet_id в tfvars
cd /root/proxy-pipeline/terraform && terraform apply -target=module.bridge
cd /root/proxy-pipeline && pipelinectl configure --bridge-only && pipelinectl links --export --qr
```

---

## Definition of Done

- [x] Токен Timeweb создан, флаг «удалять по API без Телеграма» включён.
- [x] `terraform apply` создал `twc_server.bridge` + `twc_server_ip` (IPv4 `<TW_BRIDGE_IP>`); msk-1 принят.
- [ ] `pipelinectl configure --bridge-only` / `validate --deep` зелёные; цепочка Bridge→Exit работает (внешний IP клиента = IP Exit).
- [ ] Клиентские ссылки перевыпущены и розданы.
- [ ] `enable_yandex_bridge=false`, `apply -target=module.bridge` уничтожил YC-бридж; в консоли Yandex ничего не тарифицируется.
