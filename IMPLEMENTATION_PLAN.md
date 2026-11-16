# План и фактически проверенные границы

## 1. Реализовано (локальный vertical slice)

- Изолированный stdlib-пакет `funding_collector`, запуск `python -m funding_collector`.
- Фиксированные Core/RH deployments; разрешены только два public GET маршрута. Trading/signing/withdrawal отсутствуют и явно запрещены transport.
- Intent: market, base quantity, направление обеих ног, лимиты notional/cost/book age/residual/virtual hedge delay, явные assumed fee bps.
- Раздельное metadata resolution активных perpetual, Decimal precision/min-size/min-notional/quote limits; не поддерживаемые multipliers отклоняются.
- Исполнимый проход по actual remaining order depth; обе стороны каждой книги проверены для статической all-taker roundtrip оценки. Недостаточная глубина и устаревшая receipt/cache age отклоняются.
- Два независимых EMPTY simulated accounts в одном persistent store. Реальные позиции/working orders не прочитаны; новый intent при непустом локальном inventory запрещён.
- Atomic durable state, single-writer lock, journal exports, строгий idempotency key/payload, запрет oversized/duplicate fills; offline continuation pending partial.
- Stop с known residual, при timeout — unknown hedge и possible residual bounds; никакого обещания flatten.
- Документация использования, исходный SHA и reported owner permission без выдуманной лицензии.

## 2. Синтетически проверено, НЕ является live-исполнением

Тесты в `tests_fc/` используют явно синтетические локальные fixtures. Они не импортируют старые DN adapters и не вызывают сеть.

- Каждый primary partial получает hedge до следующего primary partial; consumed ladder не используется повторно.
- Reject / timeout / outage / shutdown → stop после partial; следующий primary не создаётся.
- Reconnect → локальная synthetic ledger reconciliation; отсутствует повторная обработка fills.
- Перезапуск из сохранённого primary partial → сначала pending hedge.
- Idempotent повтор через новый Store / CLI; конфликт параметров с тем же id отклоняется.
- Oversize, duplicate fill, nonempty local inventory, min-notional/min-size/precision, cost/notional и residual/delay ограничения.
- HTTP cache Age — секунды; zero transaction time не используется как book event timestamp.
- Запрет private routes/actions и redirects по конструкции; original/private modules не импортируются.
- Public outage не создаёт фиктивных котировок, synthetic fills или state; error artifact сохраняется.
- Проверка CLI help, mandatory empty-simulation acknowledgement, state locking.

Команда полной новой безопасной suite:

```bash
python -m unittest discover -s tests_fc -v
```

Фактический текущий результат после UI-этапа: **35 tests, 8.188 s, OK** (последний повтор, Linux / Python 3.13.5; подробные timings в scratch `verification.json`). Это число unittest methods, не количество API вызовов/покрытие кода; внутри нескольких методов есть независимые subTest cases. Исходный этап: 23 tests, затем независимая ревизия довела suite до 28. Были реально получены RED до реализации пакета, domain, preview, public transport, engine, failure paths и CLI. Дополнительно реальный RED пограничного notional-at-limit-price теста и отказа hedge adapter; затем GREEN. Для UI получены RED → GREEN health/assets/entrypoint, Host/Origin/CSRF/body boundaries, preview/paper/persistent retry; отдельные boundary RED non-ASCII CSRF и giant Content-Length исправлены до GREEN. Browser RED → GREEN проверил запрет редактирования busy-form и явный expiry indicator. Уже проходившие stale/outage/timeout characterization проверки не выдаются за новые RED.

```bash
python -m compileall -q funding_collector tests_fc
git diff --check
```

Обе команды прошли. Старые DN tests/setup/live scripts не запускались: их сторонние зависимости и торговые побочные эффекты не входят в этот scope.

## 3. Реально проверено через PUBLIC MAINNET, без ключей

Финальный smoke: **2026-11-14 15:25:36.480965 UTC**, runtime **1.499159 s**, exit 0.

```bash
python -m funding_collector paper \
  --store local_runs/public-smoke-final --intent-id eth-core-rh-final \
  --market ETH --quantity 0.02 --direction core-long \
  --max-notional-usd 100 --max-roundtrip-cost-usd 2 \
  --core-fee-bps 0 --rh-fee-bps 0 \
  --max-unhedged-base 0.01 --max-hedge-delay-ms 1000 \
  --max-book-age-seconds 5 --scenario partial --assume-empty-simulated
```

Четыре настоящих GET: metadata + book отдельно Core и RH; **4 HTTP 200**. ETH perpetual независимо resolved как market 0 на обоих deployments.

| Public preview | Core | RH |
|---|---:|---:|
| Направление | Buy | Sell |
| Base qty | 0.02 ETH | 0.02 ETH |
| VWAP | 1832.08 | 1831.95 |
| Indicative notional | 36.6416 USD | 36.6390 USD |
| Min base | 0.0020 ETH | 0.0050 ETH |
| Min quote | 10 USD | 10 USD |
| Snapshot receipt/cache age | 0.480958 s | 0.480962 s |

Static entry cash cost **0.0026 USD**, full all-taker roundtrip estimate **0.0056 USD** при явном assumed fee=0 bps на каждой ноге, funding исключён. Это snapshot estimate, не realised P&L и не торговое предложение.

После настоящих market reads выполнены **только 4 synthetic fills** (0.01 Core → 0.01 RH → 0.01 Core → 0.01 RH), status `COMPLETED_SYNTHETIC`, residual **0.00 ETH**. Две paper-позиции остались открыты; реальные аккаунты не прочитаны.

Точный повтор этого же CLI после завершения: exit 0, runtime **0.684015 s**, state bytes неизменны, raw-response count остался 4, fills остались 4. Никаких дополнительных public GET на retry.

Артефакты:

- `local_runs/public-smoke-final/state.json`
- `local_runs/public-smoke-final/eth-core-rh-final/result.json`
- `local_runs/public-smoke-final/eth-core-rh-final/preview.json`
- `local_runs/public-smoke-final/eth-core-rh-final/synthetic-journal.jsonl`
- `local_runs/public-smoke-final/eth-core-rh-final/attempts/<uuid>/public/*.json`

Первый независимый успешный smoke также сохранён в `local_runs/public-smoke/`; цены между прогонами изменились. Raw body и headers сохранены, не заменены fixtures.

Дополнительно сохранён **offline replay этой же реальной книги**, без новых public reads, в `local_runs/scenario-replay/<scenario>/eth-replay/`. Каждый scenario использует независимый EMPTY synthetic store: partial и reconnect — completed, по 4 fills, residual 0.00 ETH; reject/timeout/outage/shutdown — stopped после 1 fill, residual 0.01 ETH. Timeout дополнительно сохраняет unknown hedge 0.01 ETH и possible residual bounds. Это проверка engine на замороженных данных, не новые рыночные наблюдения.

## 4. Не реализовано / не проверено / будущая отдельная авторизация

- Настоящие позиции, рабочие ордера, balances, leverage/collateral, liquidation и account fee tiers. Начальные реальные позиции НЕ известны.
- Любая подпись, API-key registration, wallet connection, private read, create/cancel/withdraw, testnet/live trade. У нового пакета нет такого кода и нет live switch.
- Биржевые WebSocket fill streams, maker queue, реальный partial/late/unknown order reconciliation, nonce/signing/account isolation на бирже, измеренный hedge latency.
- Реальная event freshness книги: public endpoint не возвращает надёжный timestamp последнего изменения. Receipt/cache age — ограниченная проверка, не доказательство свежего matching engine.
- Close/rebalance workflow, guaranteed shutdown flatten, обработка SIGTERM. Ветка KeyboardInterrupt проверена синтетическим исключением (не реальным OS SIGINT), simulated shutdown проверен. Реальный subprocess SIGKILL после durable primary/hedge fill и recovery через новый CLI теперь проверены `tests_fc/test_review.py`; внешнее exchange recovery отсутствует.
- Dynamic exit prices/basis, funding history/current estimates, scanner/ranking, strategy profitability, points eligibility. Статический fee=0 не доказывает отсутствие execution losses.
- Hyperliquid, shared remote state, hostile local users или ручная порча state. Не переиспользуйте этот simulator как live engine.
- Commit/push/PR/merge/deploy не выполнялись и этим документом не разрешаются.

## 5. Локальный UI — реализован и реально проверен 2026-11-15

Запуск из любой локальной копии репозитория:

```bash
python3 -m funding_collector serve --store local_runs/ui-paper --port 8765
```

- `web.py` использует существующие `build_preview`, `Store`, `execute`, без original DN imports и новых зависимостей. Последовательный HTTPServer только на `127.0.0.1`.
- Статические RU HTML/CSS/JS, market/direction/quantity, обязательные assumed fees, явные risk limits; separate preview / acknowledgement / paper. Busy-form блокируется, expiry явно виден.
- Host/Origin/CSRF, CSP, request JSON ≤16 KiB, строгие action fields, bounded string inputs, отказ private/arbitrary routes; в API отсутствуют fields для URL, keys, account или signing payload.
- Persistent UI draft привязан к параметрам/preview ID, execution freshness проверяется backend. Durable engine остаётся единственным источником truth; status/journal/residual/unknown/bounds и offline resume доступны после рестарта.
- `health` и `ready` реально ответили 200; readiness НЕ проверяет сеть биржи. Реальный browser прошёл оба направления public preview, explicit paper, restart и offline exact retry; desktop/mobile без горизонтального overflow. Resource failures финальной загрузки: пустой список.
- 3 public previews: ETH market 0 отдельно Core/RH, **12 HTTP 200**, assumed fee 0 bps обеих ног явно. Первый RH-long preview: VWAP Core 1792.7526 sell / RH 1793.48 buy, static roundtrip 0.006548 USD. Core-long: VWAP Core 1793.71 buy / RH 1793.85 sell, notionals 35.8742 / 35.8770 USD, static roundtrip 0.0082 USD. Третий RH-long: Core 1795.11 sell / RH 1795.43 buy, static roundtrip 0.0056 USD. Это snapshot costs, не прибыль и не fills биржи.
- Browser Core-long paper: `COMPLETED_SYNTHETIC`, 4 fills, 0.02 ETH на каждой ноге, residual 0.00, synthetic inventory Core +0.02 / RH -0.02.
- Отдельный независимый RH-long timeout paper-store: `STOPPED_SYNTHETIC`, 1 primary fill, residual -0.01 ETH, UNKNOWN hedge 0.01 ETH, bounds [-0.01, 0.00]; запрет слепого retry и no flatten видны в UI.
- Сервер действительно остановлен и запущен вновь с тем же store. Browser offline retry не изменил bytes `state.json`: SHA-256 `7783bbe4db751d94650ae474bed3b05e3b722bdb84d0aae610edf176c6a46462`; public response count до/после 8/8.
- Подробные машинные доказательства и screenshots сохранены вне Git: `/opt/data/cache/scratch/funding-collector-ui/`. `verification.json`, `actual-evidence.json`, `restart-before.json`, `restart-after.json`, `REPORT.md`; raw bodies в `browser-store/` и `timeout-store/`.
- После успешных screenshots browser screenshot daemon начал выдавать timeout на дополнительных снимках. Новая browser session успешно перепроверила persistent STOPPED UI и загрузку ресурсов; ранее сохранённые четыре screenshots доступны. Один промежуточный subprocess `serve --help` превысил 5 s при нагрузке; без изменения теста последующие повторы и полная suite прошли.

Следующий bounded этап — пользовательская локальная проверка UI и независимая ревизия safety contracts. Любой private read или live signing — отдельный дизайн, проверка полномочий и отдельное разрешение пользователя. Commit/push/PR/deploy не выполнялись.
