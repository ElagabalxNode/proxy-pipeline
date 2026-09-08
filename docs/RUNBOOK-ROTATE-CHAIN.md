# Ранбук: ротация и пересоздание цепочки YC-bridge + Exit (Cherry)

Полная **ротация старых серверов** и **пересоздание всей цепочки**
`Клиент → Bridge (Yandex Cloud) → Exit (Cherry Servers) → интернет`,
опционально **со сменой региона Exit в Cherry**.

Timeweb-бридж **выведен из эксплуатации** (сервер удалён вручную, методика отклонена).
Этот ранбук возвращает рабочую YC-цепочку (`xhttp`-relay) и чистит мёртвый Timeweb из стейта.

> Зоны ответственности: 🧑 — делаешь руками (терминал / консоли YC и Cherry), 🤖 — уже в коде.
>
> ⚠️ Это **разрушающая** операция: пересоздаются обе VM → у них **новые IP и новые ключи** →
> **все клиентские ссылки меняются** и требуют перевыпуска. Бесшовности (zero-downtime) тут нет
> by design — мы ротируем сами серверы. Окно недоступности = время cloud-init + configure (~5–10 мин).
>
> ✅ Исключение — `rotate exit`: мост остаётся, и роль моста **переиспользует его текущую identity**
> (uuid/pbk/sid/SNI/пароль SS читаются с живого моста), меняя только outbound на новый Exit.
> Клиентский `bridge_vless.png` **не меняется**, клиенты работают дальше без перенастройки.

---

## Текущий статус (live, 2026-06-25)


| Компонент                   | Факт                                                                                             | Источник                                   |
| --------------------------- | ------------------------------------------------------------------------------------------------ | ------------------------------------------ |
| **Exit**                    | Cherry `84.32.231.165`, регион `NL-Amsterdam`, id `856917`, plan `B1-1-1gb-20s-shared`           | `state/runtime.json`, `terraform output`   |
| **Bridge (YC)**             | Yandex `93.77.190.178`, id `fhmg555lh7cp8t0rd80g`, zone `ru-central1-a`, user `ubuntu`           | `terraform output`                         |
| **Bridge (Timeweb)**        | 🪦 сервер **удалён вручную**, но **ещё висит в Terraform-стейте** (`module.bridge_timeweb[0].*`) | `terraform state list`                     |
| `enable_timeweb_bridge`     | `true` (в `terraform.tfvars`) — **надо выключить**                                               | `terraform/terraform.tfvars`               |
| `state/bridge_runtime.json` | протух: показывает Timeweb-IP `72.56.38.212`, `bridge_reality_port=8443` (артефакт SS-режима)    | `state/bridge_runtime.json`                |
| `state/reality_target.json` | **отсутствует** → `configure`/`rotate exit                                                       | all`упадут, пока не пройдёт`probe-targets` |


### ⚠️ Мины, которые ломают «наивную» ротацию (читать обязательно)

> **Инцидент 2026-09-08 (`rotate exit --region SE-Stockholm`)** — две мины сработали разом:
> - **Дрейф образа пересоздал мост.** `data "yandex_compute_image" { family = "ubuntu-2204-lts" }`
>   резолвится в свежий образ при каждом plan; новый `image_id` → *forces replacement* инстанса →
>   полный `apply` внутри `rotate exit` пересоздал мост, эфемерный IP `111.88.253.165` ушёл другому
>   арендатору, все клиентские QR умерли. Закрыто: `lifecycle { ignore_changes = [image_id] }` в
>   `modules/bridge_yandex` + `rotate-chain.sh` теперь делает `plan` и **отказывается**, если план
>   уничтожает что-то вне цели ротации.
> - **Out-of-stock после destroy.** Cherry по умолчанию destroy → create; в Stockholm было 0 в
>   наличии → старый Exit уже уничтожен, новый не создан (`400 Server is out of stock`), exit
>   пропал из tfstate. Закрыто: `create_before_destroy = true` на Exit, проверка стока через
>   Cherry API до apply, и `rotate exit` умеет **создать** Exit, если его в стейте нет.
>
> Урок: у моста был **эфемерный** публичный IP, и любое пересоздание моста = новый QR у всех клиентов.
> Закрыто 2026-09-08: адрес `130.193.37.161` сделан статическим на месте (`yc vpc address update
> --reserved=true`, IP не изменился) и импортирован как `module.bridge[0].yandex_vpc_address.bridge`;
> identity моста дублируется в `state/bridge_identity.json` и восстанавливается на пересозданной VM.
> Итог: `rotate exit|bridge|all` не меняют QR; новый QR — только `rotate bridge --new-ip`.
> Цена: привязанный статический IP стоит как динамический (0.26352 ₽/ч); неиспользуемый резерв —
> 0.6039 ₽/ч (см. `vpc/pricing`).


1. **Мёртвый Timeweb в стейте отравляет оркестратор.** `pipelinectl` определяет активный мост и
  режим relay **по содержимому Terraform-стейта**:
  - `_bridge_chain_mode()` (`cli.py:30`) видит `…twc_server.bridge` → возвращает `ss` →
  YC-мост будет настроен в **Shadowsocks-режиме вместо `xhttp`** (неверно для YC).
  - `_bridge_targets()` (`cli.py:399`) приоритезирует Timeweb → `rotate bridge|all` попытается
  **уничтожить уже удалённый Timeweb-сервер** (и проигнорирует живой YC-мост). При
  `enable_timeweb_bridge=true` полный `apply` ещё и попробует **пересоздать** Timeweb (нужен `TWC_TOKEN`).
   → **Фаза 1 обязательна до любого `rotate`/`apply`:** убрать Timeweb из стейта + `enable_timeweb_bridge=false`.
2. `**probe-targets` обязателен.** Роль Exit и полный `configure` **запрещают** хардкод SNI: читают
  `state/reality_target.json` (его сейчас нет). А `pipelinectl rotate` **сам probe не запускает** →
   после смены региона возьмёт SNI старого региона. Поэтому при смене региона идём **ручным**
   путём (Сценарий B) с `probe-targets` между `apply` и `configure`.

---

## TL;DR (две траектории)

```bash
# ───────── ОБЩЕЕ: Фаза 0 (бэкап+env) и Фаза 1 (чистка Timeweb) — нужны в обоих сценариях ─────────

# ───────── Сценарий A: ротация БЕЗ смены региона (быстрый путь) ─────────
cd /root/proxy-pipeline
pipelinectl probe-targets                 # создаёт state/reality_target.json (Exit ещё старый — ок, нужен лишь SNI)
pipelinectl rotate all                    # destroy Exit+YC-bridge → apply → configure (full)
pipelinectl validate --deep
pipelinectl links --export --qr           # раздать новые ссылки

# ───────── Сценарий B: ротация СО сменой региона Cherry (ручной, рекомендуемый при смене региона) ─────────
# 1) terraform.tfvars: exit_region = "<НОВЫЙ_СЛАГ>"   (см. список ниже)
cd /root/proxy-pipeline/terraform
terraform plan            # ОЖИДАЕМ: replace module.exit (+ при -replace ниже — YC-bridge); НИ одного twc_*
terraform apply -replace='module.exit.cherryservers_server.exit' \
                -replace='module.bridge[0].yandex_compute_instance.bridge' -auto-approve
cd /root/proxy-pipeline
# дождаться SSH/cloud-init на новом Exit (~3–5 мин), затем:
pipelinectl probe-targets                 # ре-проба из НОВОГО региона → лучший SNI
pipelinectl configure                     # Exit-роль (новые ключи) + Bridge-роль (xhttp на новый Exit)
pipelinectl validate --deep
pipelinectl links --export --qr
```

Дальше — то же самое по шагам, с проверками и пояснениями.

---

## Предусловия (🧑 проверь перед стартом)

- Установлен `pipelinectl` (editable-режим из `.venv`), `terraform >= 1.5`, `ansible`.
- Ключи: `~/.ssh/proxy_pipeline` + `~/.ssh/proxy_pipeline.pub` (есть).
- Деньги на балансе **Cherry** (Exit) и **Yandex Cloud** (bridge) — пересоздаём обе VM.
- Доступ к консолям **Yandex Cloud** и **Cherry Servers** (проверить, что старое реально удалилось).
- Токены валидны:
  - Cherry: `TF_VAR_cherry_api_token` (из `.env`) и/или `terraform.tfvars`.
  - Yandex: `YC_TOKEN` (в `.env` есть; если это IAM-токен — он живёт 12 ч, **обнови при необходимости**).
  - `TF_VAR_panel_pass` / `PROXY_PIPELINE_PANEL_PASS` (из `.env`).
  - `TWC_TOKEN` **больше не нужен** (Timeweb выводим).

---

## Фаза 0 (🧑). Бэкап стейта и окружение

```bash
cd /root/proxy-pipeline

# Бэкап Terraform-стейта (откатиться, если что-то пойдёт не так)
cp terraform/terraform.tfstate "terraform/terraform.tfstate.pre-rotate.$(date +%Y%m%d-%H%M%S).bak"

# Подгрузить секреты (экспортирует TF_VAR_cherry_api_token, TF_VAR_panel_pass, YC_TOKEN, PANEL_PASS…)
source .env

# Проверка (значения не печатаем):
[ -n "$TF_VAR_cherry_api_token" ] && [ -n "$TF_VAR_panel_pass" ] && [ -n "$YC_TOKEN" ] \
  && echo "env OK" || echo "ЗАПОЛНИ .env (cherry / panel / YC_TOKEN)"
```

> Текущие клиентские ссылки (`.client-links`) сейчас указывают на **старый** мост — после
> ротации они станут нерабочими. Если нужно — заскринь/сохрани текущие ссылки до старта.

---

## Фаза 1 (🧑). Вывести мёртвый Timeweb из стейта и конфига — **обязательно**

```bash
# 1) terraform.tfvars: выключить Timeweb
#    enable_timeweb_bridge = false
#    (строку с timeweb_* можно оставить — при count=0 модуль ничего не создаёт и токен не требует)

# 2) Убрать осиротевшие Timeweb-ресурсы из стейта (сервер уже удалён вручную — НЕ destroy, а state rm,
#    чтобы Terraform не дёргал TW API и не пытался пересоздавать/удалять несуществующее)
cd /root/proxy-pipeline/terraform
terraform state list | grep timeweb            # увидеть, что именно есть
terraform state rm \
  'module.bridge_timeweb[0].twc_server.bridge' \
  'module.bridge_timeweb[0].twc_server_ip.bridge' \
  'module.bridge_timeweb[0].twc_ssh_key.bridge' \
  'module.bridge_timeweb[0].data.twc_configurator.c' \
  'module.bridge_timeweb[0].data.twc_os.ubuntu'

# 3) Проверка: в стейте остались только YC-bridge + Exit
terraform state list
#   ОЖИДАЕМ примерно:
#   module.bridge[0].data.yandex_compute_image.ubuntu
#   module.bridge[0].yandex_compute_instance.bridge
#   module.exit.cherryservers_server.exit
#   module.exit.cherryservers_ssh_key.proxy
#   terraform_data.yandex_bridge_inputs
#   (ни одной строки с timeweb / twc_)

# 4) (необязательно) убрать протухший bridge_runtime.json — его всё равно перезапишет configure
cd /root/proxy-pipeline && rm -f state/bridge_runtime.json
```

> После этого `pipelinectl` снова видит YC как активный мост, `_bridge_chain_mode()` → `xhttp`,
> `bridge_reality_port` → `443`. Мины из раздела выше обезврежены.

---

## Фаза 2 (🧑). Выбрать регион Exit (опционально) и проверить tfvars

Сменить регион — только при необходимости. Допустимые слаги (валидируются в `variables.tf`):

```
LT-Siauliai | NL-Amsterdam | US-Chicago | SG-Singapore | SE-Stockholm | DE-Frankfurt
```

Сейчас `exit_region = "NL-Amsterdam"`. Для RU-аудитории низкая задержка у EU-локаций
(`NL-Amsterdam`, `DE-Frankfurt`, `SE-Stockholm`). Лучший SNI под выбранный регион подберёт
`probe-targets` автоматически (он замеряет RTT/TLS всех `enabled`-кандидатов из
`config/reality_targets.yaml` с самого Exit).

```hcl
# terraform/terraform.tfvars
exit_region = "DE-Frankfurt"   # пример смены; либо оставь NL-Amsterdam
exit_plan   = "B1-1-1gb-20s-shared"
exit_image  = "ubuntu_24_04_64bit"

enable_yandex_bridge = true     # мост остаётся на YC
enable_timeweb_bridge = false   # из Фазы 1
```

> Актуальные слаги регионов/планов Cherry: `GET https://api.cherryservers.com/v1/regions`
> и `/v1/plans`. Если plan/region не пройдёт — Terraform упадёт сразу на `plan`.

---

## Фаза 3 (🧑). Пересоздать серверы (ротация)

### Сценарий A — без смены региона (можно через `pipelinectl rotate all`)

```bash
cd /root/proxy-pipeline
pipelinectl probe-targets         # сначала создать state/reality_target.json (configure его требует)
pipelinectl rotate all            # destroy Exit + destroy YC-bridge → full apply → configure (full)
```

`rotate all` (`cli.py:428`): таргетно уничтожает Exit и YC-bridge, делает **полный** `apply`
(пересоздаёт обе VM, outputs пересчитываются корректно), ждёт ~25 c и запускает **полный**
`configure` (роль Exit + роль Bridge). Подходит, когда SNI старого региона остаётся валидным
(регион тот же). Переходи к **Фазе 6** (validate).

> ⚠️ `rotate all` берёт `reality_target.json`, созданный твоим `probe-targets` выше (на старом
> Exit). Для **того же** региона это корректно (выбор SNI зависит от региона, а не от IP).
> Для **смены** региона так нельзя — используй Сценарий B.

### Сценарий B — со сменой региона (ручной, рекомендуемый)

```bash
cd /root/proxy-pipeline/terraform

# Сухой прогон — критическая проверка ПЕРЕД разрушением:
terraform plan
#   ОЖИДАЕМ:
#     - module.exit.cherryservers_server.exit  →  replace (из-за смены region; region у Cherry — ForceNew)
#     - НИ одного module.bridge_timeweb / twc_*  (если есть — вернись в Фазу 1)
#   Если в плане Exit помечен replace по user_data, а ты НЕ менял регион — см. «Известный нюанс» ниже.

# Пересоздать Exit (новый регион/IP) и YC-bridge (новый IP) одним applyем.
# -replace форсит ротацию YC-bridge (его конфиг не менялся); для Exit смена region уже форсит replace,
# флаг -replace на Exit избыточен, но безвреден (работает и для «того же региона»).
terraform apply \
  -replace='module.exit.cherryservers_server.exit' \
  -replace='module.bridge[0].yandex_compute_instance.bridge' \
  -auto-approve

terraform output exit_ip bridge_ip   # запомни новые IP
```

> Альтернатива `apply -replace` — классическое `terraform destroy -target=… -target=…` затем
> `terraform apply`. `-replace` короче и не оставляет окна «совсем без серверов» дольше необходимого.

---

## Фаза 4 (🧑). Ре-проба Reality-таргета с НОВОГО Exit (Сценарий B)

Дождись, пока новый Exit поднимется (SSH + cloud-init ставит 3X-UI, ~3–5 мин). Затем:

```bash
cd /root/proxy-pipeline
pipelinectl probe-targets
#   probe-targets берёт exit_ip из terraform output, ssh-ится на Exit, меряет RTT/TLS всех
#   enabled-кандидатов и пишет state/reality_target.json с победителем (лучший SNI для нового региона).
```

Если все таргеты упали по TLS — проверь сеть/файрвол нового Exit и повтори; при необходимости
включи/выключи кандидатов в `config/reality_targets.yaml`.

---

## Фаза 5 (🧑). Настроить цепочку (`configure`)

```bash
cd /root/proxy-pipeline
pipelinectl configure
```

Что делает полный `configure` (`cli.py:_do_configure`):

- пишет `ansible/inventory/hosts.ini` из `terraform output` (`exit_ip`, `bridge_ip`=YC, user=`ubuntu`);
- `chain_mode` авто = `xhttp` (Timeweb из стейта убран), `bridge_reality_port=443`;
- запускает `site.yml`: **роль Exit** (поднимает 3X-UI, генерит свежие ключи/UUID/short_id/SS-пароль,
создаёт 3 inbound'а, пишет `state/runtime.json`), затем **роль Bridge** (ставит xray, генерит
ключи моста, поднимает VLESS+Reality:443 с outbound-цепочкой `xhttp+Reality` на `Exit:chain_port`,
пишет `state/bridge_runtime.json`).

> ⚠️ Роль Exit **не идемпотентна** (добавляет inbound'ы и сбрасывает панель в `admin/admin` на старте).
> Поэтому её гоняем **только на свежесозданном** Exit (что мы и делаем). Не запускай `configure`
> повторно на уже настроенном живом Exit — будут дубли inbound'ов.

---

## Фаза 6 (🧑). Проверить цепочку

```bash
pipelinectl validate --deep
#   SSH до Exit и Bridge; TCP 443/8388/8443 на Exit; 443 на мосту; проверка хопа Bridge→Exit:chain_port;
#   --deep: логин в 3X-UI, наличие 3 inbound'ов, внешний IP Exit == exit_ip, TLS-handshake на Reality-порт.
pipelinectl doctor          # сводка из state/*.json без сети
```

> `tls_reality` в `--deep` — структурный false-positive (Reality отдаёт маскировочный сайт всем):
> «зелёный» статус там ничего не доказывает, ориентируйся на `exit_public_ip`, `panel_inbounds`,
> `bridge_to_exit_chain`.

Глубокая проверка факта проксирования (опционально, вручную): подключись клиентом по новой
ссылке и убедись, что внешний IP = IP **Exit**, а не моста.

---

## Фаза 7 (🧑). Перевыпустить и раздать клиентские ссылки

```bash
pipelinectl links --export --qr
#   пишет .client-links (профили) и .client-links-qr/*.png из свежих runtime.json + bridge_runtime.json
```

При `all`/`bridge` IP и ключи **и Exit, и моста** сменились → **все** старые ссылки/QR недействительны.
Разошли новые клиентам, дождись переключения ключевых клиентов.

При `exit` bridge-ссылки **остаются прежними** (identity моста переиспользована) — `rotate-chain.sh`
сверяет их до/после и печатает `UNCHANGED`; клиентам ничего рассылать не нужно. Меняются только
прямые (`Direct-*`) ссылки на новый Exit.

---

## Фаза 8 (🧑). Убедиться, что старые серверы удалены (нет биллинга)

- **Yandex Cloud** консоль: старая VM моста, её диск и публичный IP — удалены, новый мост поднят.
- **Cherry Servers** консоль: старый Exit (`84.32.231.165` / старый регион) удалён, новый Exit в
выбранном регионе активен. SSH-ключ `proxy-pipeline` остаётся (переиспользуется) — это норма.
- Timeweb: в панели сервера-моста быть не должно (удалён ранее вручную). Проверь, что не тарифицируется.

---

## Откат

Стейт забэкаплен в Фазе 0. Если ротация пошла не так:

```bash
cd /root/proxy-pipeline/terraform
# вернуть прежний стейт (адаптируй имя бэкапа):
cp terraform.tfstate.pre-rotate.<TS>.bak terraform.tfstate
terraform plan       # сверить, что стейт соответствует реальным ресурсам
```

⚠️ Откат стейта полезен только если соответствующие облачные ресурсы **ещё существуют**. После
фактического `destroy`/`replace` старые VM уже уничтожены — «откат» означает заново пройти ранбук
(пересоздать). Поэтому критичную проверку делаем на `terraform plan` **до** `apply`.

---

## Известные нюансы и подводные камни

- `**user_data` heredoc drift у Exit.** Полный `apply` иногда хочет `replace` Exit из-за того, что
отрендеренный `user_data` разошёлся со стейтом первого деплоя. При ротации это **нам на руку**
(Exit и так пересоздаём). Но если когда-нибудь нужно тронуть мост, **не задев** Exit — смотри план
и используй таргетные apply, либо закрой риск навсегда: `lifecycle { ignore_changes = [user_data] }`
на `cherryservers_server.exit` (`user_data` нужен только при первой загрузке).
- `**-target`/`-replace` и outputs.** При **полном** `apply` (наш случай) корневые `terraform output`
пересчитываются корректно → `configure` берёт правильные IP без `--bridge-ip`. Костыль `--bridge-ip`
нужен только после **таргетных** apply (`-target=module.bridge…`) — здесь не требуется.
- `**YC_TOKEN`.** Нужен для create/destroy YC-моста. Если это IAM-токен (живёт 12 ч) — обнови перед
стартом, иначе `apply`/`destroy` по Yandex упадёт с auth-ошибкой.
- `**rotate` и `probe-targets`.** `pipelinectl rotate` **не** запускает `probe-targets`. Для смены
региона всегда ручной путь (Сценарий B) с пробой между `apply` и `configure`.

---

## Приложение (🤖, опционально): полностью вычистить Timeweb из кода

Операционно Фазы 1 достаточно (Timeweb инертен при `enable_timeweb_bridge=false`). Если хочешь
убрать его из кода насовсем (методика отклонена) — отдельным PR:

- удалить `terraform/modules/bridge_timeweb/` (весь модуль);
- из `terraform/main.tf` — блок `module "bridge_timeweb"` и переменные `timeweb_`*;
- из `terraform/outputs.tf` — упростить `bridge_ip`/`bridge_instance_id`/`bridge_ssh_user`
до `try(module.bridge[0].…, "")` (убрать ветку `module.bridge_timeweb[0]`);
- из `terraform/variables.tf` — `enable_timeweb_bridge` и `timeweb_*`;
- из `terraform/providers.tf` и `terraform/versions.tf` — провайдер `twc`;
- удалить `terraform/probe/` и `scripts/tw_probe.sh` (эфемерные Timeweb-пробники);
- подчистить `.env`/`.env.example`/`config/pipeline.example.yaml`/`terraform.tfvars.example`
от `TWC_TOKEN`/`timeweb_*`;
- архивировать/удалить `docs/RUNBOOK-TIMEWEB.md`.

После правок: `terraform init -upgrade` (убрать провайдер из lock), `terraform validate`,
`terraform plan` (ожидаем no-op по инфраструктуре).

---

## Definition of Done

- [ ] Фаза 1: `terraform state list` не содержит `timeweb`/`twc_`; `enable_timeweb_bridge=false`.
- [ ] (если меняли) `exit_region` обновлён валидным слагом; `terraform plan` без сюрпризов.
- [ ] `terraform apply` пересоздал Exit (новый IP/регион) и YC-bridge (новый IP); outputs обновлены.
- [ ] `pipelinectl probe-targets` → свежий `state/reality_target.json` с победителем.
- [ ] `pipelinectl configure` прошёл; `state/runtime.json` + `state/bridge_runtime.json` обновлены.
- [ ] `pipelinectl validate --deep` зелёный по сути (egress IP = IP Exit, 3 inbound'а, хоп Bridge→Exit).
- [ ] `pipelinectl links --export --qr` — новые ссылки/QR розданы.
- [ ] Старые VM удалены в консолях Yandex и Cherry; биллинг по ним прекращён.