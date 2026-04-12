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
pipelinectl configure
pipelinectl validate --format json
pipelinectl links --export
pipelinectl links --export --qr
pipelinectl rotate exit
```

## Что делает каждая команда

- `pipelinectl provision` применяет Terraform и поднимает инфраструктуру.
- `pipelinectl configure` пишет inventory и запускает Ansible-настройку Exit и Bridge.
- `pipelinectl validate` проверяет SSH, порты и цепочку Bridge -> Exit.
- `pipelinectl links --export` печатает клиентские URI и пишет их в `.client-links`.
- `pipelinectl links --export --qr` дополнительно пишет PNG QR-коды в `.client-links-qr/`.
- `pipelinectl rotate exit` пересоздает Exit-ноду и затем повторно запускает `configure`.

## Что считается штатным workflow

```bash
pipelinectl provision
pipelinectl configure
pipelinectl validate
pipelinectl links --export --qr
```

Старый Bash-путь удален. Поддерживаемый control plane теперь только один: `pipelinectl` + `Terraform` + `Ansible`.

Инструкции по импорту ссылок и QR-кодов в клиенты находятся в `docs/CLIENTS.md`.
