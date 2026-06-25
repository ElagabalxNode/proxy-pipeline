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

---

## Текущий статус (live, 2026-06-25)

| Компонент | Факт | Источник |
|---|---|---|
| **Exit** | Cherry `<EXIT_IP>`, регион `NL-Amsterdam`, id `856917`, plan `B1-1-1gb-20s-shared` | `state/runtime.json`, `terraform output` |
| **Bridge (YC)** | ⚠️ **дрейф:** живой мост (на нём клиенты, egress=Exit) = **`<BRIDGE_LIVE_IP>`**; а `terraform output bridge_ip` = `<BRIDGE_TF_IP>` (id `fhmg555lh7cp8t0rd80g`), zone `ru-central1-a`, user `ubuntu`. Стейт рассинхронен с реальностью. | owner / `terraform output` |
| **Bridge (Timeweb)** | 🪦 сервер **удалён вручную**, но **ещё висит в Terraform-стейте** (`module.bridge_timeweb[0].*`) | `terraform state list` |
| `enable_timeweb_bridge` | `true` (в `terraform.tfvars`) — **надо выключить** | `terraform/terraform.tfvars` |
| `state/bridge_runtime.json` | протух: показывает Timeweb-IP `<TW_BRIDGE_IP>`, `bridge_reality_port=8443` (артефакт SS-режима) | `state/bridge_runtime.json` |
| `state/reality_target.json` | **отсутствует** → `configure`/`rotate exit|all` упадут, пока не пройдёт `probe-targets` | — |

### ⚠️ Три мины, которые ломают «наивную» ротацию (читать обязательно)

1. **Мёртвый Timeweb в стейте отравляет оркестратор.** `pipelinectl` определяет активный мост и
   режим relay **по содержимому Terraform-стейта**:
   - `_bridge_chain_mode()` (`cli.py:30`) видит `…twc_server.bridge` → возвращает `ss` →
     YC-мост будет настроен в **Shadowsocks-режиме вместо `xhttp`** (неверно для YC).
   - `_bridge_targets()` (`cli.py:399`) приоритезирует Timeweb → `rotate bridge|all` попытается
     **уничтожить уже удалённый Timeweb-сервер** (и проигнорирует живой YC-мост). При
     `enable_timeweb_bridge=true` полный `apply` ещё и попробует **пересоздать** Timeweb (нужен `TWC_TOKEN`).
   → **Фаза 1 обязательна до любого `rotate`/`apply`:** убрать Timeweb из стейта + `enable_timeweb_bridge=false`.

2. **`probe-targets` обязателен.** Роль Exit и полный `configure` **запрещают** хардкод SNI: читают
   `state/reality_target.json` (его сейчас нет). А `pipelinectl rotate` **сам probe не запускает** →
   после смены региона возьмёт SNI старого региона. Поэтому при смене региона идём **ручным**
   путём (Сценарий B) с `probe-targets` между `apply` и `configure`.

3. **YC-стейт рассинхронен с реальностью (риск осиротевшей VM с биллингом).** Живой мост =
   `<BRIDGE_LIVE_IP>`, а Terraform в стейте держит инстанс id `fhmg555lh7cp8t0rd80g` / IP
   `<BRIDGE_TF_IP>`. Возможны два случая:
   - **(a)** это **тот же** инстанс, у которого сменился NAT-IP → стейт просто протух, `terraform
     plan` его освежит и `bridge_ip` станет `<BRIDGE_LIVE_IP>`;
   - **(b)** живой `<BRIDGE_LIVE_IP>` создан **вне Terraform** (руками), а managed-инстанс
     `<BRIDGE_TF_IP>` мёртв → тогда targeted-destroy при ротации **не тронет** живую VM, а
     `apply` создаст ещё одну → **старая VM продолжит тарифицироваться** как сирота.
   → **До ротации сверь стейт с реальностью** (read-only `terraform plan`, Фаза 0а), **после
     ротации проверь консоль YC**, что осталась ровно одна (новая) bridge-VM (Фаза 8).

---

## Ротация одной командой — `scripts/rotate-chain.sh`

Обёртка, которая делает всю цепочку фаз и зашивает все грабли ниже (venv, YC-ключ,
проверка мёртвого TW в стейте, B1/B2-план под регион, `-replace` вместо «тихого» rotate,
probe между apply и configure с бэкапом, configure, `links --qr`).

Запускается одинаково через `pipelinectl rotate` (обёртка) или напрямую `scripts/rotate-chain.sh`:

```bash
cd /root/proxy-pipeline

# Пересоздать ВСЮ цепочку (Exit + YC-мост), новый QR в конце:
pipelinectl rotate all            # = scripts/rotate-chain.sh all

# Только Exit / только мост:
pipelinectl rotate exit
pipelinectl rotate bridge

# Со сменой региона (план под регион подставь сам или флагом --plan):
pipelinectl rotate all --region SE-Stockholm --plan B2-1-1gb-20s-shared

# Без интерактивного подтверждения terraform (unattended):
pipelinectl rotate all --yes
```

> `pipelinectl rotate` сам активирует `.venv` через скрипт, так что работает и из системного
> shell. Напрямую `scripts/rotate-chain.sh …` — тот же результат.

По умолчанию `terraform apply` спросит подтверждение (один раз введёшь `yes`) — это
единственная остановка. В конце скрипт печатает новые IP, URL панели и путь к QR
(**клиенту отдавать `.client-links-qr/bridge_vless.png`**). Скрипт сам:
активирует `.venv` (иначе сломанный системный ansible), берёт YC-доступ из `config/key.json`,
**отказывается** стартовать, если в стейте остался Timeweb или план Gen-1 не продаётся в
регионе, ждёт SSH+cloud-init нового Exit, ре-пробит Reality (с бэкапом прошлого
`reality_target.json`), запускает `configure` и перевыпускает ссылки/QR.

После — проверь: `source .venv/bin/activate && pipelinectl validate --deep`.

> Ниже — то же самое вручную по фазам (если что-то пошло не так в обёртке или нужен
> контроль на каждом шаге). Скрипт = автоматизация именно этих фаз.

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
- 🛑 **ОБЯЗАТЕЛЬНО активируй venv перед `pipelinectl`:** `source .venv/bin/activate`. На этом
  сервере в `/usr/bin` стоит **сломанный apt-ansible 2.10.8** (несовместим с jinja2 3.1 →
  `cannot import name 'environmentfilter'` → `wait_for_connection` зависает на 300 с и `configure`
  падает, хотя SSH открыт). `pipelinectl configure` вызывает `ansible-playbook` по PATH — venv
  (`ansible-core 2.17`) исправен, системный нет. Проверка: `which ansible-playbook` должно вести в
  `.venv/bin/`. (`probe-targets`/`validate` ходят «сырым» ssh, поэтому от бага не страдают — он бьёт
  только по ansible-шагам `configure`/`rotate`.)
- Ключи: `~/.ssh/proxy_pipeline` + `~/.ssh/proxy_pipeline.pub` (есть).
- Деньги на балансе **Cherry** (Exit) и **Yandex Cloud** (bridge) — пересоздаём обе VM.
- Доступ к консолям **Yandex Cloud** и **Cherry Servers** (проверить, что старое реально удалилось).
- Токены валидны:
  - Cherry: `TF_VAR_cherry_api_token` (из `.env`) и/или `terraform.tfvars`.
  - Yandex: **нужен ЛИБО** service-account ключ-файл, **ЛИБО** токен (одно из двух):
    - ✅ **РЕКОМЕНДУЕТСЯ:** на сервере уже есть SA-ключ **`config/key.json`** (RSA-2048, SA
      `<YC_SA_ID>`, gitignored). Не протухает. Включается через
      `export YC_SERVICE_ACCOUNT_KEY_FILE="$PWD/config/key.json"`.
    - Альтернатива: OAuth-токен (`y0_…`, ~1 год) или IAM-токен (`t1.…`, **12 ч**) в `YC_TOKEN`.
      В `.env` сейчас `YC_TOKEN` пуст (закомментирован) — это нормально, используем ключ-файл.
    - ⚠️ Не задавай ОБА сразу. Убедись, что у SA есть права на folder (`compute.editor`/`editor`).
  - `TF_VAR_panel_pass` / `PROXY_PIPELINE_PANEL_PASS` (из `.env`).
  - `TWC_TOKEN` **больше не нужен** (Timeweb выводим).

---

## Фаза 0 (🧑). Бэкап стейта и окружение

```bash
cd /root/proxy-pipeline

# Бэкап Terraform-стейта (откатиться, если что-то пойдёт не так)
cp terraform/terraform.tfstate "terraform/terraform.tfstate.pre-rotate.$(date +%Y%m%d-%H%M%S).bak"

# Подгрузить секреты. .env экспортирует: TF_VAR_cherry_api_token, TF_VAR_panel_pass,
#   PROXY_PIPELINE_PANEL_PASS, TWC_TOKEN.  YC в .env нет — авторизуемся ключ-файлом (ниже).
source .env

# Авторизация Yandex: используем SA-ключ config/key.json (рекомендуется, не протухает).
export YC_SERVICE_ACCOUNT_KEY_FILE="$PWD/config/key.json"
#   Альтернатива вместо строки выше: export YC_TOKEN="y0_…"  (OAuth) или "t1.…" (IAM, 12 ч). НЕ оба.

# Проверка. Для Yandex нужен ключ-файл ЛИБО токен; cherry/panel берутся из tfvars/pipeline.yaml:
{ { [ -n "$YC_SERVICE_ACCOUNT_KEY_FILE" ] && [ -f "$YC_SERVICE_ACCOUNT_KEY_FILE" ]; } || [ -n "$YC_TOKEN" ]; } \
  && echo "YC auth OK" || echo "❌ Нет YC-доступа: задай YC_SERVICE_ACCOUNT_KEY_FILE или YC_TOKEN"
[ -n "$TF_VAR_cherry_api_token" ] && [ -n "$TF_VAR_panel_pass" ] && echo "cherry/panel env OK" \
  || echo "ℹ️ cherry/panel env не заданы — ок, если они есть в terraform.tfvars / config/pipeline.yaml"
```

> Текущие клиентские ссылки (`.client-links`) сейчас указывают на **старый** мост — после
> ротации они станут нерабочими. Если нужно — заскринь/сохрани текущие ссылки до старта.

---

## Фаза 0а (🧑). Сверить YC-стейт с реальностью — **из-за дрейфа IP**

Живой мост = `<BRIDGE_LIVE_IP>`, а Terraform держит `<BRIDGE_TF_IP>`. Перед ротацией надо понять,
один это инстанс или два (см. мину №3). Read-only проверки (инфраструктуру **не меняют**):

```bash
cd /root/proxy-pipeline/terraform
terraform plan          # дефолтный plan делает refresh, но НЕ persist-ит стейт
#   Смотри строку про module.bridge[0].yandex_compute_instance.bridge:
#   - если plan показывает обновление nat_ip → <BRIDGE_LIVE_IP> (no-op/update in place) — случай (a):
#     managed-инстанс жив, IP просто протух. Ротация уничтожит именно его — сирот не будет.
#   - если plan хочет CREATE/replace мост (инстанса fhmg…d80g больше нет в YC) — случай (b):
#     <BRIDGE_LIVE_IP> — это VM ВНЕ Terraform. Ротация её не тронет → потом удали вручную (Фаза 8).
```

Альтернативно/в дополнение — глянь **консоль Yandex Cloud**: сколько сейчас bridge-VM, какие у них
IP и id, какая отвечает на `<BRIDGE_LIVE_IP>`. Зафиксируй id живой VM, чтобы потом не спутать.

> Если не уверен в трактовке плана — **останови ротацию и спроси/проверь руками**. Уничтожать
> живой мост вслепую нельзя (на нём клиенты), а оставить сироту — значит платить за неё.

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
(`NL-Amsterdam`, `DE-Frankfurt`, `SE-Stockholm`). SNI подбирает `probe-targets` так: он
замеряет RTT/TLS **всех `enabled`-кандидатов** из `config/reality_targets.yaml` с самого Exit и
берёт лучший по `RTT + приоритет`. Поле `regions:` в кандидатах — **только документация**, оно
**не** фильтрует выбор; «региональность» получается косвенно (RTT меряется с нового Exit). Хочешь
сузить набор под регион — правь флаги `enabled:` в `reality_targets.yaml`. (Мелочь: `exit_region`
в `state/reality_target.json` запишется как `unknown`, пока в `config/pipeline.yaml` нет
`providers.cherry.region` — на работу цепочки не влияет.)

```hcl
# terraform/terraform.tfvars
exit_region = "DE-Frankfurt"        # пример смены; либо оставь NL-Amsterdam
exit_plan   = "B2-1-1gb-20s-shared" # Cloud VPS 1 Gen2 (1c/1g/20s)
exit_image  = "ubuntu_24_04_64bit"

enable_yandex_bridge = true     # мост остаётся на YC
enable_timeweb_bridge = false   # из Фазы 1
```

> ⚠️ **План привязан к региону (мина, на которой легко словить `Plan not found`).** Тарифы
> **Gen 1 `B1-*`** (напр. `B1-1-1gb-20s-shared`) продаются только в `LT-Siauliai`, `NL-Amsterdam`,
> `US-Chicago`, `SG-Singapore`. В `DE-Frankfurt`, `SE-Stockholm`, `JP-Tokyo` есть **только Gen 2
> `B2-*`**. **`B2-1-1gb-20s-shared`** (1c/1g/20s) доступен **во всех** регионах — бери его как
> универсальный дефолт. Smallest-эквиваленты: `B2-1`(1g) / `B2-2`(2g) / `B2-4`(4g).
>
> Проверить вживую (read-only): регионы — `GET /v1/regions`; планы и их регионы —
> `GET /v1/plans` (поле `available_regions`), оба с `Authorization: Bearer $TF_VAR_cherry_api_token`
> на `https://api.cherryservers.com`. Если `exit_plan` не продаётся в выбранном `exit_region` —
> Cherry вернёт `Plan not found (400)` уже на `apply`.
>
> (NB: в `terraform/variables.tf` дефолт `exit_plan` всё ещё `B1-1-1gb-20s-shared` — устаревший;
> tfvars его перекрывает. Опционально поправь дефолт на `B2-1-1gb-20s-shared` отдельным PR.)

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
>
> ⚠️ **Тихий провал ротации моста.** В ветке `all` уничтожение YC-моста идёт с `ignore_errors=True`
> (`cli.py:431`, `subprocess … check=False`). Если destroy моста упадёт (протухший `YC_TOKEN`,
> IAM 12 ч, сетевой сбой), `rotate` это **проглотит**: последующий `apply` не увидит изменений в
> конфиге YC-инстанса и **не пересоздаст** его → мост останется со **старым IP/ключами**, но
> `configure` всё равно отработает и `rotate` завершится с кодом 0. Ты раздашь «новые» ссылки,
> которые на деле старые. **Поэтому после `rotate all` обязательно сверь, что `terraform output
> bridge_ip` реально изменился** (запиши старый IP до старта). Нужна гарантированная, громкая-при-
> ошибке ротация — иди Сценарием B с явным `terraform apply -replace=…` (он не глушит ошибки),
> даже если регион не меняешь.

### Сценарий B — со сменой региона (ручной, рекомендуемый)

```bash
cd /root/proxy-pipeline/terraform

# Сухой прогон — критическая проверка ПЕРЕД разрушением:
terraform plan
#   ОЖИДАЕМ:
#     - НИ одного module.bridge_timeweb / twc_*  (если есть — вернись в Фазу 1)
#     - смена региона обычно даёт replace у module.exit.cherryservers_server.exit (регион сервера
#       в Cherry неизменяем). Но мы НЕ полагаемся на это: ниже передаём -replace явно для ОБЕИХ нод,
#       поэтому ротация сработает, даже если план сам по себе replace не показал.
#   Если Exit помечен replace по user_data, а ты НЕ менял регион — это ожидаемо (см. «Известный нюанс»).

# Пересоздать Exit (новый регион/IP) и YC-bridge (новый IP) одним applyем.
# -replace форсит ротацию YC-bridge (его конфиг не менялся); для Exit смена region уже форсит replace,
# флаг -replace на Exit избыточен, но безвреден (работает и для «того же региона»).
terraform apply \
  -replace='module.exit.cherryservers_server.exit' \
  -replace='module.bridge[0].yandex_compute_instance.bridge' \
  -auto-approve

terraform output            # покажет все outputs (exit_ip + bridge_ip) — запомни новые IP
# (terraform output принимает максимум ОДНО имя: `terraform output exit_ip` ок, а
#  `terraform output exit_ip bridge_ip` упадёт с «Unexpected argument»)
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

> ⚠️ **Проба пишет файл даже при провале.** Неуспешный `probe-targets` всё равно перезапишет
> `state/reality_target.json` с `selected: null` (`probe.py`), и тогда `configure` откажется
> работать (нет `selected.sni`). Перед повторной пробой, если до этого был хороший файл,
> забэкапь его: `cp state/reality_target.json{,.bak}`. Идём дальше только когда у победителя
> `selected.sni` непустой (CLI печатает `winner: …`).

---

## Фаза 5 (🧑). Настроить цепочку (`configure`)

```bash
cd /root/proxy-pipeline
pipelinectl configure
```

Что делает полный `configure` (`cli.py:_do_configure`):
- пишет `ansible/inventory/hosts.ini` из `terraform output` (`exit_ip`, `bridge_ip`=YC, user=`ubuntu`);
- `chain_mode` авто = `xhttp` (Timeweb из стейта убран), `bridge_reality_port=443` (порт выбирается
  автоматически по режиму: `xhttp`/Yandex → `443`, `ss`/Timeweb → `8443` — `cli.py:92`);
- запускает `site.yml`: **роль Exit** (поднимает 3X-UI, генерит свежие ключи/UUID/short_id/SS-пароль,
  создаёт 3 inbound'а, пишет `state/runtime.json`), затем **роль Bridge** (ставит xray, генерит
  ключи моста, поднимает VLESS+Reality:443 с outbound-цепочкой `xhttp+Reality` на `Exit:chain_port`,
  пишет `state/bridge_runtime.json`).

> ⚠️ Роль Exit **не идемпотентна**: на каждом прогоне она (1) создаёт inbound'ы через
> `api/inbounds/add` (добавляет, а не обновляет → **дубли** при повторе) и (2) в начале сбрасывает
> учётку панели в `admin/admin`, а затем выставляет настоящие `panel_user/panel_pass`. Поэтому её
> запускаем **только на свежесозданном** Exit (что мы и делаем). **Не** запускай `configure`
> повторно на уже настроенном живом Exit — получишь дубли inbound'ов и временное окно `admin/admin`.

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

IP и ключи **и Exit, и моста** сменились → **все** старые ссылки/QR недействительны. Разошли новые
клиентам, дождись переключения ключевых клиентов.

---

## Фаза 8 (🧑). Убедиться, что старые серверы удалены (нет биллинга)

- **Yandex Cloud** консоль: старая VM моста, её диск и публичный IP — удалены, новый мост поднят.
  ⚠️ **Из-за дрейфа (Фаза 0а) проверь особенно тщательно:** в проекте должна остаться **ровно одна**
  bridge-VM (новая). Если живой `<BRIDGE_LIVE_IP>` был **вне Terraform** (случай b) — Terraform его
  **не** удалит; найди эту VM в консоли и **погаси вручную**, иначе она продолжит тарифицироваться.
  Сверь, что `terraform output bridge_ip` == IP той единственной живой VM, и что клиентский egress
  идёт через новый Exit.
- **Cherry Servers** консоль: старый Exit (`<EXIT_IP>` / старый регион) удалён, новый Exit в
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

- **`user_data` heredoc drift у Exit.** Полный `apply` иногда хочет `replace` Exit из-за того, что
  отрендеренный `user_data` разошёлся со стейтом первого деплоя. При ротации это **нам на руку**
  (Exit и так пересоздаём). Но если когда-нибудь нужно тронуть мост, **не задев** Exit — смотри план
  и используй таргетные apply, либо закрой риск навсегда: `lifecycle { ignore_changes = [user_data] }`
  на `cherryservers_server.exit` (`user_data` нужен только при первой загрузке).
- **`-target`/`-replace` и outputs.** При **полном** `apply` (наш случай) корневые `terraform output`
  пересчитываются корректно → `configure` берёт правильные IP без `--bridge-ip`. Костыль `--bridge-ip`
  нужен только после **таргетных** apply (`-target=module.bridge…`) — здесь не требуется.
- **`YC_TOKEN`.** Нужен для create/destroy YC-моста. Если это IAM-токен (живёт 12 ч) — обнови перед
  стартом, иначе `apply`/`destroy` по Yandex упадёт с auth-ошибкой.
- **`rotate` и `probe-targets`.** `pipelinectl rotate` **не** запускает `probe-targets`. Для смены
  региона всегда ручной путь (Сценарий B) с пробой между `apply` и `configure`.

---

## Приложение (🤖, опционально): полностью вычистить Timeweb из кода

Операционно Фазы 1 достаточно (Timeweb инертен при `enable_timeweb_bridge=false`). Если хочешь
убрать его из кода насовсем (методика отклонена) — отдельным PR:

- удалить `terraform/modules/bridge_timeweb/` (весь модуль);
- из `terraform/main.tf` — блок `module "bridge_timeweb"` и переменные `timeweb_*`;
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
