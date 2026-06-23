# Proxy Pipeline — разбор проекта, известные проблемы и следующая задача

> Документ собран по состоянию репозитория на коммите `d16ae0f` (рефактор: `Terraform` + `Ansible` + Python-оркестратор `pipelinectl`).
> Разделы: **1) разбор архитектуры**, **2) известные проблемы** (приоритизированы, проверены вживую), **3) следующая задача — перенос Bridge на Timeweb Cloud** (дешевле Yandex Cloud).

---

## 1. Разбор проекта

### Что это
Самохостируемый **двуххоповый прокси-пайплайн для обхода блокировок**. Клиент → мост в РФ (`Bridge`) → выходная нода в ЕС (`Exit`) → интернет. Весь жизненный цикл (создание сервера, настройка Xray/3X-UI, проба Reality-SNI, мониторинг, ротация IP) автоматизирован.

### Поток трафика
```
[Клиент]  --VLESS+Reality:443-->  [Bridge RU VPS]  --VLESS/xhttp+Reality:CHAIN_PORT-->  [Exit EU VPS]  --> Интернет
ShadowRocket/NekoBox/Hiddify      Xray-relay (Ansible)         3X-UI/Xray на Cherry Servers
                                                               панель 3X-UI :2053/secretpanel (plain HTTP)
```
- На клиенте — split-tunnel: RU-трафик идёт `DIRECT`, заблокированное иностранное — через прокси.
- **Bridge** принимает VLESS+Reality на `:443`, маскируясь под TLS к внешнему SNI, и весь трафик гонит outbound'ом `chain-to-exit` на `Exit:CHAIN_PORT` (VLESS поверх `xhttp`, снова в Reality, путь `/chain-relay`).
- **Exit** терминирует цепочку и выходит в интернет через `freedom`. Назначение видит IP ЕС-ноды. Есть и прямой путь: `Exit:443` (VLESS+Reality) / `Exit:8388` (Shadowsocks-2022).

### Компоненты
| Слой | Где | Назначение |
|---|---|---|
| **Оркестратор** | `orchestrator/src/pipelinectl/*.py` | CLI `pipelinectl`: `provision`, `probe-targets`, `configure`, `validate`, `doctor`, `links`, `rotate`; + Telegram-бот `pipelinebot` |
| **Terraform** | `terraform/` + `modules/exit_cherry`, `modules/bridge_yandex` | Создаёт Exit на **Cherry Servers**; опционально (`enable_yandex_bridge`) — Bridge VM в **Yandex Cloud** |
| **Ansible** | `ansible/roles/exit_xui_reality`, `ansible/roles/bridge_xray_relay` | Настройка Exit (3X-UI + inbounds) и Bridge (xray-core relay) |
| **Config** | `config/pipeline.example.yaml`, `config/reality_targets.yaml` | Конфиг пайплайна + список SNI-кандидатов для пробы |
| **State** | `state/runtime.json`, `state/bridge_runtime.json`, `state/reality_target.json` | Контракт между шагами (gitignored) |
| **CI / docs** | `.github/workflows/ci.yml`, `docs/` | fmt/validate Terraform, pytest, ruff, ansible syntax-check |

### Жизненный цикл (штатный workflow)
```bash
pipelinectl provision        # terraform apply: поднять Exit (+ Bridge, если включён)
pipelinectl probe-targets    # ОБЯЗАТЕЛЬНО до configure: с Exit измерить RTT/TLS по SNI-кандидатам
pipelinectl configure        # inventory из terraform output + ansible (Exit, затем Bridge)
pipelinectl validate         # SSH, порты, цепочка Bridge→Exit
pipelinectl links --export --qr   # клиентские URI + QR
pipelinectl rotate exit      # пересоздать Exit (новый IP) и повторно configure
```
Ключевой контракт: **`configure` определяет наличие бриджа по `terraform output bridge_ip`**. Пусто → запускается `exit_only.yml` (только Exit). Непусто → `site.yml` (Exit + Bridge), а в inventory дописывается секция `[bridge]`. Роль бриджа читает состояние Exit из `state/runtime.json`.

---

## 2. Известные проблемы

Проверено вживую: `pytest orchestrator/tests` → **15 passed, 1 FAILED**; `ruff` чисто; **CI на `main` сейчас красный**. Список приоритизирован; severity — после состязательной проверки.

### 🔴 Блокеры (чинить до push/деплоя)
| Проблема | Файл | Суть |
|---|---|---|
| Падение на свежем клоне | `orchestrator/src/pipelinectl/config.py:12` + `config/pipeline.example.yaml:54` | Нет `pipeline.yaml` (gitignored) → фолбэк на `pipeline.example.yaml`, а там **лишний backtick** → невалидный YAML → `_cfg()` без try/except роняет **каждую** команду. Он же роняет CI. |
| `1.txt` больше НЕ игнорируется | `1.txt:6`, `.gitignore` | Старое правило `*.txt` выпилили. `git status` показывает `?? 1.txt` с **живым** Cherry-токеном + JWT + 3xui-секретом → один `git add -A` публикует их. |
| Живой токен на диске в 3 местах | `.env`, `terraform/terraform.tfvars:4`, `1.txt` | `.env` и `tfvars` gitignored, но токен **живой**. Его надо **отозвать и перевыпустить** независимо от git. |
| Панель и admin-API в интернет по plain HTTP, firewall нет | `ansible/roles/exit_xui_reality/tasks/main.yml:218,264`; `terraform/modules/exit_cherry/main.tf:87` | Ни в Terraform (нет security-group), ни в Ansible (только fail2ban для sshd). Админ-пароль уходит **в открытом виде** по публичному HTTP при каждом configure и deep-validate. |
| `curl\|bash` как root + host-key checking отключён | `ansible/roles/bridge_xray_relay/tasks/main.yml:18`; `ansible/ansible.cfg:3`; `terraform/modules/exit_cherry/main.tf:62` | Xray/3x-ui тянутся без checksum/pin; `StrictHostKeyChecking=no`/`host_key_checking=False` → окно MITM→RCE на свежей VPS, через которую идут ключи и пароли. |
| deep-health «tls_reality» — структурный false-positive | `orchestrator/src/pipelinectl/deep_health.py:148` | Reality по дизайну отдаёт маскировочный сайт любому без правильных ключей → TLS-handshake против Reality-порта **проходит всегда**. «Зелёная» проверка ничего не доказывает. |
| Неуправляемая авторотация с удалённым триггером | `orchestrator/src/pipelinectl/cli.py:331`; `orchestrator/src/pipelinectl/bot.py:161` | `terraform destroy -auto-approve` без подтверждения/кулдауна/лимита; `/rotate all` доступен любому из allowlist Telegram. |
| README врёт про порядок команд | `README.md:211`; `cli.py:58` | Полный цикл в README пропускает обязательный `probe-targets` → `configure` гарантированно падает (`typer.Exit(1)`). |
| Нет remote backend | `terraform/versions.tf:1` | Токен и panel-пароль ложатся в plaintext-`terraform.tfstate` локально. |

### 🟠 Важные
| Проблема | Файл | Суть |
|---|---|---|
| Роль Exit не идемпотентна | `ansible/roles/exit_xui_reality/tasks/main.yml:67` | Повторный `configure` сбрасывает панель в `admin/admin`, затем падает на дублирующихся inbounds. |
| Шипнутый `exit_region` не проходит валидацию | `terraform/terraform.tfvars:6` + `terraform/variables.tf:18` | `NL-Ams-1` не в списке (`NL-Amsterdam`) → `terraform plan` падает сразу. Слаги регионов переименовали, tfvars/`.env` остались на старых. |
| README копирует несуществующий `terraform.tfvars.example` | `README.md:167` | Первый шаг setup падает на `cp`. |
| `rotate` пропускает `probe-targets` | `cli.py:358` | После пересоздания Exit configure либо падает, либо берёт устаревший SNI старого IP. |
| SS-2022 ссылка использует обычный base64, не url-safe | `orchestrator/src/pipelinectl/links.py:31` | Символы `+`/`/` ломают URI. |
| `run_health` хардкодит `root` | `orchestrator/src/pipelinectl/health.py:98` | Игнорирует `exit_user` → ложный FAIL для non-root Exit. |
| Секреты в командной строке shell-тасок без `no_log` | `ansible/roles/exit_xui_reality/tasks/main.yml:335` | Приватный ключ, UUID, SS-пароль могут попасть в вывод/логи. |

### 🟡 Мелочи (кратко)
URL-encoding в VLESS/SS ссылках не делается (`links.py:23`); scratch-файлы с креденшалами пишутся с дефолтным umask, не 0600 (`exit_xui_reality/tasks/main.yml:274`); парсинг x25519-ключа моста ломкий (`bridge_xray_relay/tasks/main.yml:42`); `tcp_bridge_443` всегда severity `error` (`health.py:114`); `pick_winner` кидает `KeyError` вместо `RuntimeError` (`probe.py:116`); temp-файл с паролем для ansible `-e` не 0600 (`ansible_runner.py:46`); `/links` и `/qr` отдают bearer-секреты в Telegram (`bot.py:193`); тесты местами поверхностные (`test_contract.py:50`); `terraform validate` в README без `init` (`README.md:295`).

### Что рефактор реально улучшил
Утечка секретов в git закрыта (`.gitignore` покрывает `.env`, `config/pipeline.yaml`, `state/`, `*.tfstate`, `*.pem`, `*.key`; в отслеживаемых файлах живых секретов нет); секреты подаются через env (`PROXY_PIPELINE_PANEL_PASS`, `TF_VAR_*`), `config.py` их не логирует; в Ansible есть `no_log`/`sensitive=true`; появились тесты, CI, ruff, модульный Terraform и обязательный гейт `probe-targets`.

---

## 3. Следующая задача: перенести Bridge на Timeweb Cloud

> Заменяет прежний план «совместить бридж с backend сайта» — от него отказались (конфликт по порту 443 и риск общего IP). Вместо этого — **отдельная дешёвая VM в Timeweb Cloud** вместо дорогой в Yandex Cloud.

### Зачем
| Провайдер | Конфиг (пример) | Цена/мес |
|---|---|---|
| Yandex Cloud (сейчас) | 2 vCPU / 2 GB / 20 GB | **≈ 3500 ₽** |
| Timeweb Cloud (цель) | 1 vCPU / 1 GB / 15 GB NVMe | **≈ 800 ₽** \* |

Экономия **≈ 2700 ₽/мес (~75%)**. Bridge — лёгкий xray-relay, ему достаточно 1 vCPU / 1 GB. Timeweb — **российский провайдер с РФ-локациями** (СПб/Москва), что для RU-бриджа идеально (низкая задержка до клиентов). *\*точная цена зависит от выбранного тарифа/локации.*

### Что Timeweb даёт для автоматизации
- **Terraform-провайдер** `timeweb-cloud/timeweb-cloud` (публичный реестр; вендор также раздаёт зеркало `tf.timeweb.cloud/timeweb-cloud/timeweb-cloud` — на случай недоступности публичного реестра).
- **Auth**: переменная окружения `TWC_TOKEN` (или `provider "twc" { token = "..." }`). Токен — в панели Timeweb → раздел API.
- ⚠️ **Критично для ротации/destroy**: у токена должно быть **отключено «подтверждение удаления через Telegram»**, иначе `terraform destroy` и `pipelinectl rotate` будут падать.
- **Ресурсы**: `twc_server`, `twc_ssh_key`, `twc_vpc`, `twc_project`, `twc_server_ip`. **Data sources**: `twc_os` (по `name`+`version`), `twc_configurator` (по `location`+`disk_type` → `id` для кастомной конфигурации), `twc_presets`, `twc_ssh_keys`.
- Ключевые поля `twc_server`: `name`, `os_id`, **либо** `preset_id`, **либо** блок `configuration { configurator_id, cpu, ram, disk }`, `ssh_keys_ids`, `availability_zone`, опц. `cloud_init`. Экспортирует **`main_ipv4`** (публичный IP — это наш `bridge_ip`), `id`, `status`.
- Есть и **REST API** (`api.timeweb.cloud`) — если понадобятся скрипты вне Terraform.

### Решение: модуль `bridge_timeweb` (минимально-инвазивно)
Добавляем модуль по образцу `bridge_yandex`, переключаемый флагом `enable_timeweb_bridge`. Модуль отдаёт **те же** outputs, что и YC-бридж (`external_ip`, `instance_id`, `ssh_user`), а корневые `bridge_ip/bridge_instance_id/bridge_ssh_user` берут значение из активного модуля (Timeweb приоритетнее). Тогда:
- **Роль `bridge_xray_relay` не меняется вообще** — она провайдеро-независима (всё берёт из `state/runtime.json` + SSH).
- `pipelinectl configure / validate / links` работают без изменений (читают только `bridge_ip` и т.п.).
- В оркестраторе меняется только target для `rotate bridge` (см. ниже).

#### Файлы модуля (готовый HCL)
`terraform/modules/bridge_timeweb/versions.tf`
```hcl
terraform {
  required_providers {
    twc = {
      source  = "timeweb-cloud/timeweb-cloud"
      version = "~> 1.2"
    }
  }
}
```
`terraform/modules/bridge_timeweb/variables.tf`
```hcl
variable "ssh_public_key_path" {
  type = string
}
variable "name_prefix" {
  type    = string
  default = "proxy-pipeline"
}
variable "location" {
  type    = string
  default = "ru-1" # РФ-локация Timeweb (СПб); ru-2 и др. — уточнить в панели
}
variable "disk_type" {
  type    = string
  default = "nvme"
}
variable "cpu" {
  type    = number
  default = 1
}
variable "ram_mb" {
  type    = number
  default = 1024
}
variable "disk_mb" {
  type    = number
  default = 15360
}
variable "os_name" {
  type    = string
  default = "ubuntu"
}
variable "os_version" {
  type    = string
  default = "22.04"
}
```
`terraform/modules/bridge_timeweb/main.tf`
```hcl
data "twc_os" "ubuntu" {
  name    = var.os_name
  version = var.os_version
}

data "twc_configurator" "c" {
  location  = var.location
  disk_type = var.disk_type
}

resource "twc_ssh_key" "bridge" {
  name = "${var.name_prefix}-bridge"
  body = trimspace(file(var.ssh_public_key_path))
}

resource "twc_server" "bridge" {
  name              = "${var.name_prefix}-bridge"
  os_id             = data.twc_os.ubuntu.id
  availability_zone = var.location
  ssh_keys_ids      = [twc_ssh_key.bridge.id]

  configuration {
    configurator_id = data.twc_configurator.c.id
    cpu             = var.cpu
    ram             = var.ram_mb
    disk            = var.disk_mb
  }
}
```
`terraform/modules/bridge_timeweb/outputs.tf`
```hcl
output "external_ip" {
  value = twc_server.bridge.main_ipv4
}
output "instance_id" {
  value = twc_server.bridge.id
}
output "ssh_user" {
  value = "root" # Timeweb даёт root-доступ по ssh-ключу
}
```
> Альтернатива конфигуратору — фиксированный тариф через `data "twc_presets"` + `preset_id` (если 800 ₽ соответствует конкретному пресету). Конфигуратор выбран как детерминированный (явные cpu/ram/disk).

#### Правки корневого стека
`terraform/versions.tf` — добавить провайдер в `required_providers`:
```hcl
    twc = {
      source  = "timeweb-cloud/timeweb-cloud"
      version = "~> 1.2"
    }
```
`terraform/providers.tf` — добавить:
```hcl
# Token: env TWC_TOKEN (или token = "..."). У токена выключите Telegram-подтверждение удаления.
provider "twc" {}
```
`terraform/variables.tf` — флаг и параметры:
```hcl
variable "enable_timeweb_bridge" {
  type    = bool
  default = false
}
variable "timeweb_location" {
  type    = string
  default = "ru-1"
}
variable "timeweb_cpu" {
  type    = number
  default = 1
}
variable "timeweb_ram_mb" {
  type    = number
  default = 1024
}
variable "timeweb_disk_mb" {
  type    = number
  default = 15360
}
```
`terraform/main.tf` — добавить модуль рядом с `module "bridge"`:
```hcl
module "bridge_timeweb" {
  count  = var.enable_timeweb_bridge ? 1 : 0
  source = "./modules/bridge_timeweb"

  ssh_public_key_path = var.ssh_public_key_path
  location            = var.timeweb_location
  cpu                 = var.timeweb_cpu
  ram_mb              = var.timeweb_ram_mb
  disk_mb             = var.timeweb_disk_mb
}
```
`terraform/outputs.tf` — отдавать IP активного бриджа (Timeweb приоритетнее YC; `try`-цепочка не падает, если бридж выключен совсем):
```hcl
output "bridge_ip" {
  value = try(module.bridge_timeweb[0].external_ip, try(module.bridge[0].external_ip, ""))
}
output "bridge_instance_id" {
  value = try(module.bridge_timeweb[0].instance_id, try(module.bridge[0].instance_id, ""))
}
output "bridge_ssh_user" {
  value = try(module.bridge_timeweb[0].ssh_user, try(module.bridge[0].ssh_user, ""))
}
```

#### Правка оркестратора (одна строка)
`pipelinectl rotate bridge` хардкодит YC-адрес ресурса — для Timeweb он другой.
`orchestrator/src/pipelinectl/cli.py:341`
```python
# было (YC):
["module.bridge[0].yandex_compute_instance.bridge"]
# для Timeweb:
["module.bridge_timeweb[0].twc_server.bridge"]
```
Лучше — выбирать target по тому, какой модуль включён (из конфига/`bridge_instance_id`), чтобы поддержать оба провайдера без правки кода.

### Ранбук миграции YC → Timeweb
Предусловия: Exit уже поднят и настроен (`state/runtime.json` есть); SSH-ключ `~/.ssh/proxy_pipeline(.pub)`; токен Timeweb с **выключенным** Telegram-подтверждением удаления.

0. **Бэкап** `terraform/terraform.tfstate`.
1. **Создать токен Timeweb** (панель → API), `export TWC_TOKEN=...`. Проверить, что Telegram-подтверждение удаления выключено.
2. **Добавить файлы модуля** и правки корневого стека (HCL выше).
3. `cd terraform && terraform init` — скачает провайдер `twc`.
4. В `terraform.tfvars`: **включить Timeweb, выключить YC**:
   ```hcl
   enable_yandex_bridge  = false
   enable_timeweb_bridge = true
   timeweb_location      = "ru-1"
   ```
5. `terraform plan` → убедиться, что создаётся `twc_server.bridge` (+`twc_ssh_key`) и уничтожается `yandex_compute_instance.bridge`. `terraform apply`.
   - *Минимальный простой*: чтобы без окна — на шаге 4 временно оставьте `enable_yandex_bridge=true` тоже (Timeweb приоритетен в outputs); пройдите шаги 6–8, раздайте новые ссылки, и только потом отдельным `apply` выключите YC.
6. `pipelinectl configure` — впишет Timeweb-IP в inventory `[bridge]` (user `root`) и прогонит роль `bridge_xray_relay` на новом бридже (новые ключи/UUID).
7. `pipelinectl validate` (и `--deep`) — SSH, порты 443/8388, цепочка Bridge→Exit.
8. `pipelinectl links --export --qr` — перевыпустить клиентские ссылки (IP и ключи бриджа сменились) и раздать клиентам.
9. **Убедиться, что YC-бридж уничтожен** и не тарифицируется (консоль Yandex Cloud: VM, диск, NAT-IP). При желании — удалить YC-модуль/провайдер из кода.

### Откат
`enable_timeweb_bridge=false`, `enable_yandex_bridge=true` (+ YC-переменные/`YC_TOKEN`) → `terraform apply` → `pipelinectl configure` → `pipelinectl links --export --qr`. Вернуть строку target в `cli.py`.

### Проверить при внедрении (открытые вопросы)
- **Тариф под бюджет**: 1 vCPU / 1 GB / 15 GB — дешёвый baseline; если 800 ₽ соответствует конкретному пресету — взять его через `data.twc_presets` + `preset_id`. Свериться с актуальным прайсом Timeweb.
- **Локации**: `ru-1` (СПб) подтверждена примером провайдера; уточнить доступные слаги (`ru-2` и др.) под нужный город.
- **Версия провайдера**: закреплено `~> 1.2` — свериться с актуальной в реестре.
- **Блокировки**: как любой RU-бридж, Timeweb-IP может попасть под блокировку — ради этого в пайплайне и есть ротация (`pipelinectl rotate bridge` после правки target). Фронта/CDN перед бриджем здесь нет.

### Definition of Done
- [ ] Создан модуль `terraform/modules/bridge_timeweb` (4 файла) и внесены правки в `versions.tf`/`providers.tf`/`variables.tf`/`main.tf`/`outputs.tf`.
- [ ] `terraform init` скачал провайдер `twc`; `terraform validate`/`fmt -check` зелёные.
- [ ] `TWC_TOKEN` задан, Telegram-подтверждение удаления у токена выключено.
- [ ] `enable_timeweb_bridge=true`, `enable_yandex_bridge=false`; `terraform apply` создал Timeweb-бридж и уничтожил YC-бридж.
- [ ] `pipelinectl configure`/`validate` прошли; цепочка Bridge→Exit работает (внешний IP клиента = IP Exit).
- [ ] Клиентские ссылки перевыпущены и раздан­ы.
- [ ] YC-бридж в консоли не тарифицируется; `cli.py` target для `rotate bridge` обновлён на `twc_server`.
