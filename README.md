# Proxy Pipeline

Разворачивает и обслуживает цепочку прокси из двух серверов:

```text
Клиент ──HTTPS (свой домен)──▶ Мост (RU) ──REALITY/XHTTP──▶ Exit (за рубежом) ──▶ Интернет
         VLESS + XHTTP + TLS      nginx + Xray                 3X-UI + Xray
```

Эталонная связка в этом репозитории:

| Узел | Где | Чем управляется |
|---|---|---|
| Exit | Cherry Servers | Terraform создаёт и пересоздаёт сервер, Ansible настраивает 3X-UI |
| Мост | любой RU VPS (здесь VDSina), создаётся вручную | Ansible ставит Xray и nginx |
| DNS и сертификат | домен в Timeweb Cloud, Let's Encrypt | `pipelinectl dns` и `pipelinectl cert` через API Timeweb |

Всем управляет консольная утилита `pipelinectl` на вашей машине — контроллере. Клиентская ссылка
содержит имя домена, а не IP, поэтому при замене любого узла она остаётся прежней.

## Что понадобится

- Контроллер с Linux: Python ≥ 3.10, Terraform ≥ 1.5, `ssh`, `curl`, `git`.
- Аккаунт Cherry Servers: API-токен и ID проекта.
- RU VPS с Ubuntu 22.04/24.04 и доступом `root` по SSH-ключу.
- Домен, DNS которого обслуживает Timeweb Cloud, и API-токен Timeweb с доступом к DNS.

## Установка — по шагам

**1. Код и утилита**

```bash
git clone https://github.com/ElagabalxNode/proxy-pipeline.git && cd proxy-pipeline
python3 -m venv .venv && . .venv/bin/activate
pip install -e "orchestrator[dev]"
```

**2. SSH-ключ контроллера.** Им пользуются Terraform (кладёт его на новый Exit) и Ansible.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/proxy_pipeline -N ""
ssh-copy-id -i ~/.ssh/proxy_pipeline.pub root@<IP_МОСТА>     # на RU VPS
```

**3. Секреты** (в git не попадают):

```bash
cp .env.example .env        # CHERRY_API_TOKEN, PROXY_PIPELINE_PANEL_PASS, TIMEWEBCLOUD_AUTH_TOKEN
set -a; . ./.env; set +a    # перед каждой сессией
```

**4. Настройки**

```bash
cp config/pipeline.example.yaml config/pipeline.yaml
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
```

Обязательно заполнить:

| Файл | Ключ | Что это |
|---|---|---|
| `terraform.tfvars` | `cherry_project_id`, `exit_region`, `exit_plan` | где и какой Exit создавать (`pipelinectl stock` покажет наличие) |
| `terraform.tfvars` | `xui_version`, `xui_archive_sha256` | закреплённый релиз 3X-UI для Exit |
| `pipeline.yaml` | `xray.world_release` | та же версия 3X-UI, версия и SHA256 Xray внутри архива (`pipelinectl world-release --archive <tar.gz>`) |
| `pipeline.yaml` | `xray.bridge_release` | версия и SHA256 архива Xray для моста |
| `pipeline.yaml` | `bridge_manual.ip` | адрес RU VPS |
| `pipeline.yaml` | `bridge_entry.domain`, `zone` | имя входа, например `lk.example.ru` |
| `pipeline.yaml` | `acme.lego_release` | версия и SHA256 lego для выпуска сертификата |
| `pipeline.yaml` | `firewall.direct_policy` | кто может ходить на Exit напрямую: `public`, `allowlist` или `closed` |

**5. Развернуть**

```bash
pipelinectl provision                     # Terraform создаёт Exit
pipelinectl probe-targets                 # подбор маскировочного сайта для REALITY
pipelinectl dns ensure                    # A-записи домена → мост
pipelinectl cert issue                    # wildcard-сертификат Let's Encrypt
pipelinectl configure --bootstrap-panel   # Exit и мост; флаг нужен только для новой панели
pipelinectl status                        # все узлы должны быть OK
pipelinectl links --qr                    # ссылки в .client-links, QR в .client-links-qr/
```

Клиенту нужна ссылка `Bridge-XHTTP-…`. Проверенные клиенты: Shadowrocket (iOS), v2rayN (Windows);
отпечаток TLS в клиенте — `firefox` (с `chrome` соединения рвутся).

`configure` ставит на контроллер таймеры: каждые 5 минут журнал проверок забирается с моста, а
`pipelinectl watchdog` при сбое шлёт оповещение (Telegram, e-mail, SMS — секция `alerts`).

## Если что-то отвалилось

**Шаг 1 — понять, что сломано:**

```bash
pipelinectl status
```

Команда проверяет четыре части цепочки, ничего не меняя, и для сломанной печатает команду починки:

```text
[OK  ] Exit (WORLD)   Cherry Servers
[DOWN] Bridge (RU)    VDSina
         - only 6 of 20 Russian networks reach the bridge address (address filtered)
         fix 1: create a new RU server with the controller's SSH key (provider panel)
         fix 2: pipelinectl rotate bridge --bridge-ip NEW_IP
[OK  ] DNS name       Timeweb DNS
[OK  ] Certificate    Let's Encrypt via DNS-01
```

Кроме проверок с контроллера и с самого моста, `status` спрашивает около 20 российских сетей
(Globalping), открывается ли мост снаружи. Так находится случай, когда на мосту всё зелёное, а
клиенты не могут подключиться, потому что адрес фильтруется в пути. Отключить: `--no-outside`.

**Шаг 2 — починить:**

| Сломано | Команда | Что произойдёт |
|---|---|---|
| Exit | `pipelinectl rotate exit --yes` | Новый Exit в Cherry, проверки достижимости с моста и репутации IP, настройка, перенастройка моста. Если нет в наличии: `pipelinectl stock`, затем `--region <SLUG>` |
| Мост: сервер умер или адрес фильтруется | создать новый RU VPS с ключом контроллера, затем `pipelinectl rotate bridge --bridge-ip <НОВЫЙ_IP>` | Мост ставится на новый сервер с прежней identity и сертификатом, DNS переводится на него, `bridge_manual.ip` обновляется |
| Мост: сервер жив, но вход не отвечает | `pipelinectl configure --bridge-only` | Переустановка конфигурации Xray/nginx на мосту |
| DNS | `pipelinectl dns ensure --replace` | A-записи домена снова указывают на мост |
| Сертификат | `pipelinectl cert renew --deploy` | Продление и доставка на мост |

После любой починки — снова `pipelinectl status`. Ссылки у клиентов не меняются; DNS-кеш на
устройствах обновляется в течение ~10 минут (до этого можно временно указать IP в поле «Адрес»
профиля, оставив SNI и Host с доменом).

Новый адрес моста до переноса можно проверить из российских сетей: на нём должен отвечать HTTPS.
Для VDSina есть помощник `.venv/bin/python scripts/vdsina_ip.py overview | check <IP>`.

## Команды

| Команда | Назначение |
|---|---|
| `pipelinectl status` | какой узел сломан и чем чинить |
| `pipelinectl validate [--deep]` | все проверки подробно |
| `pipelinectl chainlog [--since 7d]` | история проверок с моста: когда и что падало |
| `pipelinectl rotate exit\|bridge` | заменить узел |
| `pipelinectl configure [--bridge-only]` | применить конфигурацию к узлам |
| `pipelinectl links [--qr]` | клиентские ссылки и QR |
| `pipelinectl dns show\|ensure` | DNS-записи домена моста |
| `pipelinectl cert status\|issue\|renew` | сертификат входа |
| `pipelinectl stock` | наличие серверов Cherry по регионам |
| `pipelinectl ipcheck <IP>` | репутация адреса (чужие сервисы, malware) |
| `pipelinectl inventory` | версии и настройки на узлах, сверка с пинами |
| `pipelinectl backup` | резервная копия контроллера и узлов перед изменениями |
| `pipelinectl alert-check --dry-run` | что сейчас ушло бы в оповещения |

## Где что лежит

```text
config/pipeline.yaml        настройки (gitignored); пример — pipeline.example.yaml
terraform/                  Exit на Cherry (и устаревшие модули мостов Yandex/Timeweb)
ansible/roles/              exit_xui_reality, exit_firewall, bridge_xray_relay
orchestrator/               pipelinectl (Python)
scripts/rotate-chain.sh     ротация, которую вызывает pipelinectl rotate
state/                      состояние после configure: адреса, ключи, сертификат (gitignored)
backups/                    резервные копии pipelinectl backup (gitignored)
docs/                       журнал работ (SPRINT-1.md) и ранбуки
```

## Безопасность

- `.env`, `config/pipeline.yaml`, `terraform/terraform.tfvars`, tfstate, `state/`, `backups/` и
  `.client-links*` содержат ключи и пароли и исключены из git. Не коммитьте их и не публикуйте
  реальные адреса и домен моста в открытых местах.
- Панель 3X-UI наружу не открыта: доступ через `ssh -L 2053:127.0.0.1:2053 root@<EXIT>`.
- Автоматическая замена Exit при сбое выключена (`watchdog.auto_rotate: false`), потому что
  создание сервера стоит денег; включите её, если нужна.

## Проверка кода

```bash
cd orchestrator && python -m pytest -q && ruff check src tests
cd ../terraform && terraform fmt -check && terraform validate
```
