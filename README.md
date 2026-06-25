# Proxy Pipeline

Автоматизированный пайплайн для развёртывания и обслуживания цепочки:

```text
Client -> Bridge (RU) -> Exit (EU) -> Internet
```

Текущая реализация больше не опирается на ручные Bash-скрипты. Контрольная плоскость теперь состоит из:

- **Terraform**: инфраструктура Exit на Cherry Servers и опционального Bridge в Yandex Cloud
- **Ansible**: конфигурация 3X-UI / Xray на уже поднятых ВМ
- **pipelinectl**: единая CLI-оркестрация `provision -> configure -> validate -> links -> rotate`

## Архитектура

```text
[Client] --VLESS/Reality--> [Bridge RU] --xhttp--> [Exit EU] --> [Internet]
   |                            |                    |
   | ShadowRocket / NekoBox     | Yandex Cloud       | Cherry Servers
   | Hiddify / Podkop           | (optional IaC)     | (Terraform-managed)
   |                            |                    |
   +----------------------------+--------------------+
                                |
                          [3X-UI on Exit]
```

## Что является источником истины

- `terraform/` — декларативное состояние инфраструктуры
- `config/pipeline.yaml` — локальная конфигурация пайплайна
- `terraform output -json` — контракт между Terraform и оркестратором
- `state/runtime.json` — runtime-артефакт после настройки Exit
- `state/bridge_runtime.json` — runtime-артефакт после настройки Bridge

Bash-автоматизация удалена. Если вы видите старую инструкцию со `scripts/*.sh`, она больше не актуальна.

## Структура проекта

```text
terraform/                  # root stack + modules
terraform/modules/exit_cherry/
terraform/modules/bridge_yandex/
ansible/playbooks/          # exit_only.yml / site.yml
ansible/roles/              # exit_xui_reality / bridge_xray_relay
orchestrator/               # пакет pipelinectl
config/pipeline.example.yaml
docs/PIPELINE.md            # outputs/runtime contract
state/                      # локальные runtime-артефакты (gitignored)
```

## Как это работает

### 1. Provision

`pipelinectl provision` запускает `terraform init` и `terraform apply` в `terraform/`.

Root stack поднимает:

- модуль `exit_cherry`
- модуль `bridge_timeweb` если `enable_timeweb_bridge=true` (рекомендуемый RU-бридж)
- модуль `bridge_yandex` если `enable_yandex_bridge=true` (legacy / fallback)

Ключевые outputs:

- `exit_ip`
- `exit_id`
- `panel_url`
- `ssh_command`
- `bridge_ip`
- `bridge_instance_id`
- `bridge_ssh_user`

Подробный контракт описан в [`docs/PIPELINE.md`](docs/PIPELINE.md).

### 2. Configure

`pipelinectl configure`:

1. читает `terraform output -json`
2. генерирует `ansible/inventory/hosts.ini`
3. запускает:
   - `ansible/playbooks/exit_only.yml`, если Bridge не включён
   - `ansible/playbooks/site.yml`, если есть Exit + Bridge

Exit role:

- ждёт маркер готовности cloud-init
- генерирует Reality x25519 keypair
- логинится в 3X-UI
- создаёт inbound'ы:
  - `VLESS-Reality-Vision`
  - `SS2022-Fallback`
  - `Chain-Bridge-Entry`
- пишет `state/runtime.json`

Bridge role:

- устанавливает Xray
- включает BBR
- рендерит `config.json` для Xray relay
- пишет `state/bridge_runtime.json`

### 3. Validate

`pipelinectl validate` проверяет:

- SSH к Exit
- TCP-порты Exit (`reality`, `ss`, `chain`)
- SSH к Bridge, если он есть
- порт `443` на Bridge
- доступность `Bridge -> Exit:chain_port`

Есть текстовый и JSON-формат:

```bash
pipelinectl validate
pipelinectl validate --format json
```

### 4. Client Links

`pipelinectl links` собирает URI из `state/runtime.json` и `state/bridge_runtime.json`.

- прямые ссылки на Exit
- bridge-ссылки, если Bridge поднят

По умолчанию:

- выводит ссылки в stdout
- пишет `.client-links`

Примеры:

```bash
pipelinectl links
pipelinectl links --format json
pipelinectl links --no-export
```

### 5. Rotate

`pipelinectl rotate` пересоздаёт ноды и сразу перевыпускает ссылки/QR — тонкая обёртка над
`scripts/rotate-chain.sh` (одна команда на всю ротацию). Делает: активацию `.venv`, Yandex-auth
из `config/key.json`, защиту от мёртвого Timeweb в стейте и несовместимого с регионом плана,
`terraform apply -replace`, ожидание SSH, ре-пробу Reality, `configure` и `links --qr`.

```bash
pipelinectl rotate all                  # Exit + YC-мост (по умолчанию)
pipelinectl rotate exit                 # только Exit
pipelinectl rotate bridge               # только мост
pipelinectl rotate all --region SE-Stockholm --plan B2-1-1gb-20s-shared   # со сменой региона
pipelinectl rotate all --yes            # без интерактивного подтверждения terraform

# Эквивалентно напрямую:
scripts/rotate-chain.sh all
```

По умолчанию `terraform apply` спрашивает подтверждение один раз. В конце печатается новый QR —
клиенту отдавать `.client-links-qr/bridge_vless.png`. Подробности и ручные фазы:
[`docs/RUNBOOK-ROTATE-CHAIN.md`](docs/RUNBOOK-ROTATE-CHAIN.md).

## Быстрый старт

### 1. Установите зависимости

- Terraform >= 1.5
- Python >= 3.10
- Ansible
- `curl`
- `jq`
- `ssh`

### 2. Подготовьте конфиг

```bash
cp config/pipeline.example.yaml config/pipeline.yaml
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
```

`config/pipeline.yaml` хранит локальные настройки пайплайна.  
`terraform/terraform.tfvars` хранит параметры инфраструктуры.

### 3. Передайте секреты через environment

Минимум для Exit:

```bash
export TF_VAR_cherry_api_token="..."
export TF_VAR_panel_pass="..."
export PROXY_PIPELINE_PANEL_PASS="$TF_VAR_panel_pass"
```

Опционально для Timeweb Bridge (рекомендуемый RU-бридж, дешевле Yandex):

```bash
export TWC_TOKEN="..."   # панель Timeweb -> «API и Terraform»
```

В `terraform/terraform.tfvars`:

```hcl
enable_timeweb_bridge         = true
timeweb_configurator_location = "ru-2"   # ru-1=СПб, ru-2=Москва
timeweb_availability_zone     = "msk-1"  # "" если аккаунт не даёт выбрать зону
```

⚠️ У токена Timeweb включите «удалять сервисы по API без кода в Телеграме», иначе `terraform destroy` / `pipelinectl rotate` зависнут. Подробный ранбук миграции: [`docs/RUNBOOK-TIMEWEB.md`](docs/RUNBOOK-TIMEWEB.md).

Опционально для Yandex Bridge (legacy / fallback):

```bash
export YC_TOKEN="..."
export YC_CLOUD_ID="..."
export YC_FOLDER_ID="..."
```

Если включаете Yandex Bridge, в `terraform/terraform.tfvars` задайте:

```hcl
enable_yandex_bridge = true
yandex_folder_id     = "..."
yandex_subnet_id     = "..."
yandex_zone          = "ru-central1-a"
```

Важно: модуль Yandex Bridge ожидает **готовый subnet с NAT / внешним доступом** (Timeweb-модуль этого не требует — публичный IP выдаётся серверу). Создание VPC и security group автоматически ещё не добавлено.

### 4. Установите `pipelinectl`

```bash
cd orchestrator
python3 -m pip install -e ".[dev]"
```

### 5. Выполните полный цикл

```bash
pipelinectl provision
pipelinectl configure
pipelinectl validate
pipelinectl links
```

## Конфигурация

### `config/pipeline.yaml`

Ключевые секции:

- `paths`
- `providers`
- `ssh`
- `panel`
- `xray`
- `alerts`

Пример лежит в [`config/pipeline.example.yaml`](config/pipeline.example.yaml).

### `terraform/terraform.tfvars`

Ключевые переменные:

- `cherry_project_id`
- `exit_region`
- `exit_plan`
- `exit_image`
- `enable_timeweb_bridge`, `timeweb_configurator_location`, `timeweb_availability_zone`, `timeweb_cpu`, `timeweb_ram_mb`, `timeweb_disk_mb`
- `enable_yandex_bridge`
- `yandex_folder_id`
- `yandex_subnet_id`
- `yandex_zone`

Полный набор описан в [`terraform/variables.tf`](terraform/variables.tf).

## Terraform layout

### Exit

Модуль [`terraform/modules/exit_cherry`](terraform/modules/exit_cherry):

- `cherryservers_ssh_key.proxy`
- `cherryservers_server.exit`
- bootstrap через `user_data`
- вычисление primary IPv4 через `ip_addresses`

### Bridge

Два взаимозаменяемых модуля, выбираются флагами `enable_*_bridge`. Оба отдают
одинаковые outputs (`external_ip`, `instance_id`, `ssh_user`), поэтому роль
`bridge_xray_relay`, `configure`/`validate`/`links` от провайдера не зависят. В
outputs Timeweb приоритетнее Yandex (полезно при бесшовном переключении).

Модуль [`terraform/modules/bridge_timeweb`](terraform/modules/bridge_timeweb) — RU-бридж на Timeweb Cloud (рекомендуемый):

- `twc_server.bridge` + `twc_ssh_key.bridge`
- размер через `configuration { configurator_id, cpu, ram, disk }` (ram/disk в МБ)
- публичный IPv4 — `main_ipv4` (отдельный платный ресурс у Timeweb, выдаётся серверу)
- SSH user `root`

Модуль [`terraform/modules/bridge_yandex`](terraform/modules/bridge_yandex) — legacy / fallback:

- `yandex_compute_instance.bridge`
- Ubuntu image family
- NAT IP через `network_interface`
- SSH metadata user `ubuntu`

## Ansible layout

### `exit_xui_reality`

Роль [`ansible/roles/exit_xui_reality`](ansible/roles/exit_xui_reality):

- ждёт `/opt/proxy-pipeline/.status`
- извлекает x25519 через Xray binary
- создаёт inbound payloads
- пишет runtime contract

### `bridge_xray_relay`

Роль [`ansible/roles/bridge_xray_relay`](ansible/roles/bridge_xray_relay):

- ставит Xray
- пишет `/usr/local/etc/xray/config.json`
- перезапускает systemd service
- публикует bridge runtime metadata

## Проверка и качество

Локальные проверки:

```bash
pytest -q orchestrator/tests
ruff check orchestrator/src orchestrator/tests
cd terraform && terraform fmt -check && terraform validate
```

CI находится в [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Текущие ограничения

- Exit role пока не делает полноценный reconciliation существующих inbound'ов в 3X-UI; повторный `configure` на уже настроенном Exit требует осторожности.
- Bridge-модуль не создаёт сеть и security group с нуля; нужен существующий subnet.
- Python health-check пока не включает Telegram-уведомления.
- В README и коде подразумевается стандартный Yandex Ubuntu user `ubuntu`.

## Практические замечания

- Не храните токены в git. `terraform/terraform.tfvars`, `config/pipeline.yaml` и `state/*.json` игнорируются не случайно.
- Если ранее токены уже попадали в репозиторий или в логи, перевыпустите их.
- Для Cherry используйте **API slugs** регионов, а не названия из UI.

## Регионы Cherry Servers

Используйте slug из API `GET https://api.cherryservers.com/v1/regions`.

| Slug | Город | Назначение |
|------|-------|------------|
| `SE-Stockholm` | Stockholm | Рекомендованный Exit |
| `NL-Amsterdam` | Amsterdam | Альтернативный Exit |
| `DE-Frankfurt` | Frankfurt | Альтернативный Exit |
| `LT-Siauliai` | Lithuania | Запасной Exit |
| `US-Chicago` | Chicago | Редко для Exit |
| `SG-Singapore` | Singapore | Редко для Exit |
