# funding_collector — локальный DN hedge executor

## Браузерный LIVE-прототип для VPS

Новый отдельный web-live путь: защищённый вход владельца → конфигурация двух ног →
зашифрованные dedicated execution keys → read-only account-check → fresh ARM →
существующий isolated signer/worker → подтверждённые fills и журнал.
Это **не** прежний paper UI из команды `funding_collector serve` ниже.

Локальный запуск web-live (Linux, отдельное окружение):

```sh
python3.13 -m venv .venv-live-web
.venv-live-web/bin/pip install -r requirements-live-web.lock
.venv-live-web/bin/pip check
.venv-live-web/bin/python -m funding_collector.live_web init --root "$HOME/.local/share/funding-collector/web-live"
.venv-live-web/bin/python -m funding_collector.live_web serve --root "$HOME/.local/share/funding-collector/web-live" --port 8765
```

Первый `init` запрашивает пароль владельца через masked terminal prompt; ключи затем
настраиваются в авторизованном браузере. Для VPS обязателен HTTPS через отдельный
Caddy proxy; 8765 не публикуется. Подробности, ограничения и Docker Compose:
**[deployment/live-web/README.md](deployment/live-web/README.md)**.

Офлайн-контракты и synthetic browser acceptance не подтверждают реальную биржевую
регистрацию/ордера. Docker image build, реальный TLS/VPS и реальные account/order
acceptance проверяются отдельно. Нет автоматического cancel/flatten: logout и expiry
закрывают authority, но позиции и ордера могут остаться на биржах.


## Быстрый локальный запуск UI (без ключей)

Из корня клонированного репозитория на Linux:

```bash
python3 -m funding_collector serve --store local_runs/ui-paper --host 127.0.0.1 --port 8765
```

Откройте **http://127.0.0.1:8765/**. Для Core/RH public/paper нужны только Python 3.10+ и stdlib; не запускайте legacy setup/controller. Комиссии в форме — явные допущения, не тариф аккаунта. Web UI выполняет только симуляцию, приватный worker запускается отдельно из терминала. Запуск UI на текущей машине проверен реальным браузером: public preview → полный primary LIMIT → один synthetic hedge → сохранённый журнал.

## Текущий статус

- Прежний paper UI (`funding_collector serve`): public market data и synthetic исполнение. Новый отдельный `funding_collector.live_web` предоставляет явно вооружаемый браузерный LIVE-путь; он описан выше.
- Отдельный локальный терминальный `live`-контур: Core, RH, native Hyperliquid, Entropy, Extended и Aster. Проверен офлайн с dummy-ключами и подменёнными транспортами; это **не допуск к реальным средствам**.
- Режим: полный набор primary LIMIT → один opposite protected IOC по подтверждённому base quantity → сверка fills и остатка. IOC не гарантирует полный fill. Автоматических повторных hedge/flatten нет.
- Entropy: отдельные identity/agent credentials; reviewed private pair только `io:NVDA` против Core/RH `NVDA`. Не любые рынки HIP-3.
- Extended: явный `fee_rate_cap` в public config; комиссии USD отдельно от USDC. Зависимости `requirements-extended.lock` устанавливаются в отдельное окружение; основная native/Core среда не содержит всех Extended SDK.
- EVEDEX **отложен**. Public/paper сохранены, экспериментальный Node private bridge не интегрирован в общий Python worker и не считается готовым торговым подключением.
- Перед реальным использованием нужны независимый security review, отдельно разрешённый read-only account-check и отдельное согласование минимальных торговых тестов. `live unlock` может отправить реальные ордера после локального ARM.

Ниже сохранена документация этапов public/paper и legacy-режима; формулировки «без ключей» и «не умеет торговать» относятся к этому режиму, **не ко всему репозиторию**. Upstream DN/controller также может выполнять реальные действия; не запускайте его для установки нового приложения.


> **Уточнение области:** описание public/paper и UI ниже не включает отдельный
> терминальный `live`-контур native Hyperliquid / Lighter Core. В нём реализованы
> encrypted setup, read-only `account-check` и явно вооружаемый supervised worker;
> проверены только офлайн-контракты с фиктивными аккаунтами/транспортами.
> Это не live-допуск. Установка, безопасный onboarding и оставшиеся ограничения:
> [LOCAL_TEST_WALLET.md](LOCAL_TEST_WALLET.md), зависимости —
> `requirements-live-native-core.lock`. В UI приватные ключи и live ARM не доступны.

## Aster Futures V3

В UI/CLI площадка `aster` использует точные символы `BTCUSDT`, `ETHUSDT`, `SOLUSDT`;
публичный стакан и mark price читаются без ключей. Paper fills остаются синтетическими.
Variational не добавлен в новый public/paper/private путь: наличие legacy adapter
не означает готовую интеграцию.

Для отдельного терминального `live` используются существующие encrypted custody,
signer child, factory и общий worker. Установите `requirements-live-native-core.lock`
в отдельный venv; он содержит `eth-account` для V3 EIP-712 подписи. Протокол проверен
по официальному [aster-finance API](https://github.com/asterdex/api-docs), ревизия
`f3d1c073f63c90d54766e0317e10627ce8031a38`; реальный private API пока не проверен.

Отличия Aster в публичной конфигурации из [LOCAL_TEST_WALLET.md](LOCAL_TEST_WALLET.md):
- deployment: `aster-mainnet`; `account` — owner/account address, `signer` — адрес
  **отдельного** зарегистрированного agent signer, `api_key_index: null`;
- private `symbol`: `BTC`, `ETH` или `SOL`; локальные reviewed `asset_id`: 0, 1, 2
  соответственно — **не биржевые asset IDs**; wire-symbol сохраняется отдельно;
- секрет — private key только dedicated agent, не основного кошелька; ввод только
  в masked terminal prompt. Agent permissions на бирже могут быть шире локальных;
- нужен уже настроенный one-way mode и разрешённый `canTrade`. `registration()`
  только читает состояние и ничего не регистрирует/не меняет на бирже;
- primary — post-only `LIMIT GTX`, hedge — защищённый ценой `LIMIT IOC`.
  ACK не подтверждает fill; fills сверяются отдельно, комиссии — `USDT`,
  без неявного сложения с `USDC`/`USD`;
- USDT collateral/settlement не равны USDC: общая USD-оценка индикативна,
  stablecoin parity не гарантируется;
- один workload на dedicated signer/journal. UNKNOWN блокирует новые отправки,
  нет автоматического retry, cancel, flatten, transfers или настройки аккаунта;
- public smoke не доказывает доступность аккаунта, права agent, принятие ордера или
  fills. Реальные read-only проверки и минимальные сделки требуют отдельного
  согласования; публикация кода не является разрешением на торговлю.

## Hyperliquid и Entropy — отдельные подключения

`hyperliquid` — native perpetuals, `entropy` — HIP-3 namespace `io`. В UI отдельные выбор, статусы, paper inventory и области будущих account/agent/nonce настроек. Ключи или баланс между ними не переносятся. Если позднее использовать одинаковый signer для обеих площадок, протокольный nonce остаётся общим: требуются отдельные agent keys или явно согласованная координация.

Hyperliquid public preview проверен для ETH против Core. Только проверенные BTC/ETH/SOL base units; совместимость base quantity не означает совпадение oracle, collateral, funding и settlement. Сопоставление с Entropy разрешается только для отдельно доказанного эквивалентного контракта — native и `io:*` не взаимозаменяемы. MARKET у HL — защищённый ценой LIMIT IOC, возможен partial/reject. Отправка реальных ордеров, agent setup и приватные account reads не реализованы; параметры ключей в UI не вводятся.

Пример текущего DN v3 (реальные public quotes, synthetic fills, комиссии явно предполагаются):

```bash
python -m funding_collector dn-paper --store local_runs/hl-paper --intent-id hl-eth-001 \
  --primary-venue hyperliquid --hedge-venue core --market ETH --hedge-market ETH \
  --direction long --quantity 0.02 --primary-fee-bps 0 --hedge-fee-bps 0 \
  --max-notional-usd 100 --max-roundtrip-cost-usd 5 --max-book-age-seconds 30 \
  --assume-empty-simulated
```

## Актуальный режим DN v3 и EVEDEX

Основной UI использует **полный набор первой ноги LIMIT → один противоположный MARKET** по подтверждённому base quantity, только в симуляции. Примеры `paper` ниже относятся к сохранённому legacy-режиму с хеджем каждого partial; для текущего сценария используйте `dn-paper` или UI. Старые ledger не меняют режим автоматически.

EVEDEX подключён к production public API через отдельный официальный Node SDK. Из корня репозитория:

```bash
npm ci --ignore-scripts --prefix optional/evedex
python -m funding_collector serve --store local_runs/ui-paper --port 8765
```

Выберите EVEDEX и точный инструмент `ETHUSD` или `BTCUSD`; проверенные контрплощадки — Core/RH с `ETH` или `BTC`. Остальные инструменты/пары пока отклоняются. Комиссии вводятся явно в bps, не считываются с аккаунта. Публичные книги настоящие; все fills остаются synthetic.

**Торговое подключение EVEDEX отложено:** экспериментальный Node private bridge существует, но общий Python execution path не интегрирован. API key нельзя считать строго read-only: API допускает отдельные account mutations. Подпись one-click delegate и защищённый LIMIT IOC проверены лишь в отдельном offline-тесте; это не готовность реального account/worker пути. SDK MARKET использует `cashQuantity`; paper MARKET нельзя выдавать за гарантированный base-quantity hedge.

Для Core/RH Node не нужен. Для EVEDEX нужен установленный Node.js и указанные optional-зависимости. Ключи не вводите в UI, `.env`, аргументы CLI или чат.

**Новый CLI не умеет торговать.** Он читает публичные metadata/книги Lighter Core и Lighter RH, проверяет hedge intent и при явном выборе `paper` создаёт только синтетические fills. Ключи, кошельки, SDK и приватные endpoints не нужны и не используются.

```bash
# Из корня вашей локальной копии funding_collector:
python -m funding_collector --help
python -m unittest discover -s tests_fc -v
```

Python 3.10+ и stdlib, Linux/macOS (`fcntl.flock`; Windows — через WSL); фактически проверено на Python 3.13.5 / Linux. Для нового пакета **не устанавливайте старый `requirements.txt` и не запускайте `scripts/setup.sh`, `scripts/doctor.sh`, `scripts/run_enter.sh` или `controller`**: это сохранённый upstream DN с отдельным, потенциально live-исполнением.

## Локальный web UI · без ключей

Из корня вашей локальной копии репозитория (путь может быть любым):

```bash
python3 -m funding_collector serve --store local_runs/ui-paper --port 8765
```

Откройте **http://127.0.0.1:8765/** в браузере на этой же машине. Пакеты из pip/npm, wallet connection и API keys не нужны. `localhost`, внешний Host, LAN bind и произвольные Origin намеренно не разрешены. Другой локальный порт задаётся `--port`; без reverse proxy и remote deployment. Если порт занят, выберите свободный порт, сохраняя тот же `--store` для той же пары paper-аккаунтов.

1. Выберите точный perpetual symbol (ETH/BTC/SOL — только подсказки), Core-long или RH-long, одинаковую **base quantity на каждой ноге** и intent ID. Market ID и активный контракт проверяются независимо на обеих площадках.
2. Явно введите assumed fees обеих ног в **bps**; поля изначально пусты. Укажите per-leg notional, полный static roundtrip cost, unhedged-base, virtual hedge-delay и receipt/cache-age limits. Начальные значения лимитов — редактируемые примеры, не рекомендация риска.
3. **«Получить public preview»** делает реальные разрешённые GET: metadata + книга Core и RH. Показываются стороны, VWAP, notional, worst price, consumed visible depth, slippage, assumed fees и static entry/exit/roundtrip costs. Funding не включён. GET failure не подменяется синтетическими котировками.
4. Проверьте результат, отметьте допущение **EMPTY LOCAL SIMULATION** и отдельно нажмите **«Выполнить ТОЛЬКО paper»**. До этого ни одного synthetic fill. Paper расходует замороженную книгу preview; просроченный receipt/cache-age блокирует запуск, изменённые параметры требуют нового preview. Event freshness биржи не подтверждается.
5. Журнал показывает `COMPLETED_SYNTHETIC` либо `STOPPED_SYNTHETIC`, число fills, residual, unknown hedge, его bounds и stop reason. `timeout` специально демонстрирует UNKNOWN + failure-stop, без скрытого flatten. Открытые paper-позиции остаются открытыми; это не реальный вход и не прибыль.

После перезапуска той же командой UI читает тот же durable `state.json`. В журнале **«Показать сохранённый preview»** восстанавливает параметры для просмотра; **«Продолжить / точный повтор · offline»** сверяется с durable ledger и продолжает только незавершённую симуляцию. Completed/STOPPED не создают новые fills. Точный повтор paper также идемпотентен; смена параметров с тем же ID отклоняется. Новый intent при непустом локальном inventory запрещён; не меняйте store, пытаясь это обойти. Разные stores — только независимые эксперименты, не реальные subaccounts.

Локальная диагностика (без сети бирж):

```bash
curl http://127.0.0.1:8765/api/health
curl http://127.0.0.1:8765/api/ready
```

`health` проверяет процесс; `ready` проверяет открытие/версию/lock ledger, **не** доступность бирж или свежесть котировок. Остановка: Ctrl+C; web-сервер не закрывает paper-позиции автоматически.

### Реализовано в UI

- Один hedge intent, отдельные preview/paper действия, оба направления Core/RH, явные fees/risk limits.
- Public-only данные и синтетическое исполнение помечены отдельно; journal/status/inventory/residual/unknown доступны после рестарта.
- Только literal loopback `127.0.0.1`; точный Host с портом; same-origin проверки; обязательные Origin + per-process CSRF для POST. CORS отсутствует. CSP без inline scripts, без внешних ресурсов; данные выводятся как текст, не HTML.
- Строгие поля API без путей/URL/credentials; JSON body ≤16 KiB, запрет transfer encoding/duplicate Content-Length, socket timeout. Нельзя передать произвольный signing payload или приватный route. Единственный последовательный HTTP worker + существующий single-writer ledger lock.

### Не реализовано / границы прототипа

- Любые подписи, wallets, private/account reads, real/testnet orders, leverage/collateral/liquidation, live enable switch.
- Funding scanner/ranking/доходность/points, maker queue, измеренная exchange latency/event freshness, close/rebalance и guaranteed flatten.
- Удалённый доступ, TLS/authentication для нескольких пользователей, защита от вредоносного локального пользователя/расширения/процесса или ручной порчи ledger. Loopback+CSRF не являются sandbox или защитой от локального владельца OS. Сервер последовательный: медленный public GET временно задерживает другие запросы; не рассчитан на production нагрузку.
- Windows без WSL не проверен и не поддержан текущим `fcntl` store; macOS совместимость предполагается, фактически проверен только Linux. UI без JS не работает.

Raw public ответы и `preview.json` лежат в `<store>/<id>/attempts/<uuid>/`; UI draft `<store>/<id>/ui-draft.json` — только привязка preview к параметрам, **не** источник fill recovery. Источник истины — `state.json`. Перезапуск меняет CSRF-токен: перезагрузите страницу. Перед переносом paper ledger сохраняйте store целиком.

## Один hedge intent, без реальных ордеров

```bash
python -m funding_collector paper \
  --store local_runs/my-paper-accounts \
  --intent-id eth-example-001 \
  --market ETH --quantity 0.02 --direction core-long \
  --max-notional-usd 100 --max-roundtrip-cost-usd 2 \
  --core-fee-bps 0 --rh-fee-bps 0 \
  --max-unhedged-base 0.01 --max-hedge-delay-ms 1000 \
  --max-book-age-seconds 5 --scenario partial \
  --assume-empty-simulated
```

`--quantity` — одинаковое количество базового актива **на каждой ноге**, не сумма долларов. `core-long` = купить Core / продать RH; `rh-long` = продать Core / купить RH. Core остаётся первичной ногой симулятора в обоих направлениях. `--core-fee-bps` и `--rh-fee-bps` — ваши явные допущения, не проверенный тариф аккаунта. 1 bp = 0.01%; 0 в примере — допущение, не обещание бесплатного исполнения.

`--store` представляет одну пару локальных paper-аккаунтов, изначально пустых. Настоящие позиции, balances и working orders **не прочитаны**. Не меняйте store/id, пытаясь обойти защиту: новый intent при непустом локальном inventory запрещён. Закрытие/перебалансировка пока не реализованы. Отдельный store допустим только для действительно независимого эксперимента.

Полностью такой же вызов `paper` с тем же id и параметрами — идемпотентный повтор; fills не добавляются и сеть не запрашивается. Изменённые параметры с прежним id отклоняются. Сохранённые значения параметров сравниваются строго, включая строковое представление чисел.

```bash
# Только чтение локального состояния:
python -m funding_collector status --store local_runs/my-paper-accounts --intent-id eth-example-001
# Offline-продолжение незавершённой СИМУЛЯЦИИ по сохранённой книге:
python -m funding_collector resume --store local_runs/my-paper-accounts --intent-id eth-example-001
```

`resume` не обновляет рынок и не обещает пригодность старой книги для реального исполнения. Завершённый или остановленный intent не запускается заново. Для расчёта без fills используйте тот же полный вызов с командой `preview`; `--assume-empty-simulated` нужен именно для `paper`.

## Что проверяется

- Два независимых deployment: Core `https://mainnet.zklighter.elliot.ai/api/v1` (chain 304), RH `https://api.rh.lighter.xyz/api/v1` (chain 466324). Это идентификаторы, signing отсутствует.
- Только GET `orderBookDetails` и `orderBookOrders?market_id=…&limit=100`; редиректы и произвольные параметры запрещены. `create_order`, `cancel_order`, `withdraw`, `sign` всегда отказывают.
- Точный symbol активного perpetual выбирается отдельно на каждом deployment. Spot, неоднозначность, frozen/reduce-only и non-unit multipliers не принимаются.
- Decimal, шаг количества/цены, min size, min notional, биржевой quote limit, пользовательский per-leg notional limit (также по worst/limit price), полный статический roundtrip cost.
- Сортировка отдельных ордеров и проход по `remaining_base_amount`, не по initial size и не только по best price. Недостаточная видимая глубина → отказ; 100 ордеров не выдаются за всю книгу.
- Возраст snapshot ограничен по локальному времени начала GET и HTTP Date/Age. Endpoint не даёт надёжного времени последнего изменения книги: **свежесть exchange-event не подтверждается**. `transaction_time=0` и order expiry не используются как время книги.
- Статическая all-taker оценка отдельно показывает entry cash cost, fees, spread/depth, exit estimate. Funding не загружается и не включён, будущие цены/basis/fees не гарантированы. Это не прогноз прибыли и не оценка maker-стратегии.

## Сценарии только синтетического исполнения

`--scenario partial|reject|timeout|reconnect|outage|shutdown`.

`partial`: два детерминированных partial fill первичной ноги, если шаг допускает разбиение. Каждый хеджируется до следующего fill; книга расходуется без повторного использования глубины. В симуляторе hedge заполняется целиком для каждого полученного partial. Это не реалистичная модель биржевой очереди.

`reject` / `outage` / `shutdown`: stop после первого partial; остаток записан, автоматическое flatten не изображается. `timeout`: исход hedge неизвестен, сохраняется потенциальный диапазон остатка; слепой повтор запрещён. `reconnect`: только локальная сверка синтетического durable ledger, не WebSocket/реальная account reconciliation. `--max-hedge-delay-ms` проверяется по виртуальным 1 ms (обычно) / 5 ms (reconnect), **не по измеренной биржевой задержке**. Невозможность захеджировать partial из-за minima/precision тоже приводит к stop с остатком. Успешный hedge оставляет две противоположные paper-позиции открытыми.

Коды выхода: 0 = preview/успех/чтение; 2 = отказ параметров, данных или сети; 3 = synthetic failure-stop; 130 = interruption вне engine. SIGINT внутри engine сохраняет stop без гарантии flatten. SIGKILL/падение восстанавливается из durable state; отдельная обработка SIGTERM не реализована.

## Локальные артефакты

```text
local_runs/<store>/
  .lock                         # один writer (Linux flock)
  state.json                    # durable истина: intents + accounts/inventory + journal
  <intent-id>/
    attempts/<uuid>/public/*.json # реальные raw response body, URL, headers, timestamps/errors
    attempts/<uuid>/preview.json # public preview этого чтения
    attempts/<uuid>/error.json   # если сеть/валидация не прошла
    preview.json
    result.json
    synthetic-journal.jsonl
```

State сохраняется atomic replace + fsync; JSONL/result — восстановимые проекции. SIGKILL между экспортами не является поводом повторять уже записанный fill: используйте `resume`. Храните весь store; ручное удаление state или другой store сбрасывает только локальную защиту, не реальные позиции. Network failure никогда не подменяется mock-котировкой. Raw данные публичные, но могут содержать публичные account indexes участников книги. Артефакты исключены из Git.

План и границы проверок: [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). Происхождение DN и границы permission: [ATTRIBUTION.md](ATTRIBUTION.md).
