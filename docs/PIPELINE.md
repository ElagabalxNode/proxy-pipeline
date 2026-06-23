# Автоматизация пайплайна (`Terraform` + `Ansible` + `pipelinectl`)

## Контракт выходных данных Terraform

После `terraform apply` корневой стек отдает такие значения через `terraform output -json`:

| Output | Описание |
|--------|----------|
| `exit_ip` | Публичный IPv4 Exit-ноды |
| `exit_id` | Идентификатор сервера в Cherry Servers |
| `panel_url` | URL панели `3X-UI` |
| `ssh_command` | Подсказка для SSH-подключения к Exit |
| `bridge_ip` | Публичный IP Bridge-ноды в Yandex Cloud (пусто, если `enable_yandex_bridge=false`) |
| `bridge_instance_id` | Идентификатор Bridge VM (пусто, если Bridge отключен) |
| `bridge_ssh_user` | SSH-пользователь для Bridge (`ubuntu` для стандартного Ubuntu-образа) |

## Контракт runtime-состояния (`state/runtime.json`)

Этот файл записывается Ansible после настройки Exit-ноды. Затем он используется:

- ролью Bridge
- `pipelinectl validate`
- `pipelinectl links`

Основные поля: `exit_ip`, `uuid`, `public_key`, `private_key`, `short_id`, `ss_password`, `chain_uuid`, `panel_url`, `reality_sni`, `reality_port`, `ss_port`, `ss_method`, `chain_port`.

## Конфигурация

1. Скопируйте пример:

```bash
cp config/pipeline.example.yaml config/pipeline.yaml
```

2. Передайте секреты через переменные окружения. Рекомендуемый минимум:

- `CHERRY_API_TOKEN`
- `TF_VAR_cherry_api_token`
- `TF_VAR_panel_pass`
- `PROXY_PIPELINE_PANEL_PASS`

Для Yandex Cloud дополнительно:

- `YC_TOKEN`
- `YC_CLOUD_ID`
- `YC_FOLDER_ID`

И не забудьте включить Bridge и заполнить параметры Yandex в `terraform.tfvars`:

- `enable_yandex_bridge=true`
- `yandex_folder_id`
- `yandex_subnet_id`

## Основные команды

```bash
cd orchestrator && python3 -m pip install -e ".[dev]"
pipelinectl --help
pipelinectl provision
pipelinectl probe-targets          # обязателен до configure
pipelinectl configure
pipelinectl validate --format json
pipelinectl validate --deep
pipelinectl doctor
pipelinectl links --export
pipelinectl links --export --qr
pipelinectl rotate exit
```

## Что делает каждая команда

- `pipelinectl provision` применяет Terraform и поднимает инфраструктуру.
- `pipelinectl probe-targets` запускается **с локальной машины после provision**: через SSH на Exit VPS измеряет RTT и TLS-хендшейк для каждого включённого кандидата из `config/reality_targets.yaml` и записывает результат в `state/reality_target.json`. Команда `configure` **не стартует** без этого файла (нет молчаливого fallback-а на `www.microsoft.com`).
- `pipelinectl configure` пишет inventory и запускает Ansible-настройку Exit и Bridge.
- `pipelinectl validate` проверяет SSH, порты и цепочку Bridge -> Exit.
- `pipelinectl validate --deep` добавляет вход в панель 3X-UI, список inbound'ов, проверку публичного IP Exit через ipify и TLS-handshake к Reality-порту с `server_hostname=reality_sni` (без новых зависимостей).
- `pipelinectl doctor` печатает сводку из `state/*.json` и файлов ссылок **без сетевых запросов**.
- `pipelinectl links --export` печатает клиентские URI и пишет их в `.client-links`.
- `pipelinectl links --export --qr` дополнительно пишет PNG QR-коды в `.client-links-qr/`.
- `pipelinectl rotate exit` пересоздает Exit-ноду и затем повторно запускает `configure`.

## Что считается штатным workflow

```bash
pipelinectl provision
pipelinectl probe-targets
pipelinectl configure
pipelinectl validate
pipelinectl links --export --qr
```

### `pipelinectl probe-targets` подробнее

- Читает `config/reality_targets.yaml` (канонический список кандидатов SNI).
- Через SSH на Exit VPS запускает Python 3 `ssl`+`socket` пробы для каждого `enabled: true` target.
- Оценивает результаты: TLS OK → оценка 100 − RTT_ms + (2 − priority)×10.
- Записывает `state/reality_target.json`:

```json
{
  "selected": {
    "id": "addons-mozilla",
    "sni": "addons.mozilla.org",
    "dest": "addons.mozilla.org:443",
    "score": 94.0,
    "measured_rtt_ms": 6.0,
    "tls_ok": true,
    "alpn": "h2",
    "timestamp": "2026-04-13T10:23:00Z",
    "exit_ip": "1.2.3.4",
    "exit_region": "NL-Amsterdam"
  },
  "candidates_tested": 8,
  "candidates_passed": 6
}
```

- `configure` читает `selected.sni` → `reality_sni`, `selected.dest` → `reality_dest` и передаёт в Ansible.
- Ansible падает явно, если `reality_sni` или `reality_dest` пусты.

Старый Bash-путь удален. Поддерживаемый control plane теперь только один: `pipelinectl` + `Terraform` + `Ansible`.

Инструкции по импорту ссылок и QR-кодов в клиенты находятся в `docs/CLIENTS.md`.

## Telegram Bot (`pipelinebot`)

Опционально: установите зависимость и заполните секцию `telegram` в `config/pipeline.yaml` (см. `config/pipeline.example.yaml`):

```bash
cd orchestrator && pip install -e ".[telegram]"
export TELEGRAM_BOT_TOKEN="..."   # или `telegram.bot_token` в YAML
pipelinebot
```

- `telegram.allowed_user_ids` — whitelist числовых Telegram user id (обязательно, иначе бот не стартует).
- Команды: `/status`, `/validate`, `/deep`, `/rotate exit|bridge|all`, `/links`, `/qr`, `/help`.
- Долгие команды (`/deep`, `/rotate`) сериализуются через один `asyncio.Lock`.

Разовая отправка сообщения **без** polling (например, из cron или watchdog):

```bash
pipelinectl notify "Exit пересоздан, новый IP: …"
```

Нужны `TELEGRAM_BOT_TOKEN` (или `telegram.bot_token`) и `telegram.notify_chat_id` (или `TELEGRAM_NOTIFY_CHAT_ID`).
