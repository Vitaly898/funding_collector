# Локальная проверка native Hyperliquid / Lighter Core

**Статус: офлайн-проверенная реализация, не live-допуск.** Реальные ключи, приватные account reads, реальные/testnet ордера и wallet approvals при этой проверке не использовались. Независимый security review и отдельно разрешённая проверка реального аккаунта остаются блокерами выпуска. Web UI остаётся paper/public-only; локальный терминальный `live` — отдельный исполняющий контур, не кнопка браузера.

## Установка только на своей машине

Из корня своей копии репозитория, без абсолютных путей разработчика:

```sh
python3.13 -m venv .venv-native-core
.venv-native-core/bin/python -m pip install -r requirements-live-native-core.lock
.venv-native-core/bin/python -m pip check
.venv-native-core/bin/python -m unittest discover -s tests_fc -v
node --test tests_fc/ui_semantics.test.cjs
# Необязательно: только если нужен отдельный EVEDEX public bridge.
npm ci --ignore-scripts --prefix optional/evedex
npm test --prefix optional/evedex
```

Проверены Python 3.13.5 / Linux x86_64 и Node 26.7.0. Lock — точный снимок проверенного изолированного окружения, без локальных/editable путей; это не hash-locked supply-chain artifact. Перенос на другую ОС/архитектуру и чистая установка на машине пользователя ещё не проверены. Нужен Linux (поддержка `prctl`, `fcntl` и signer child isolation); macOS не заявлен для этого private-контура. Не смешивайте с `requirements.txt`, `requirements-extended.lock`, `controller` и legacy scripts. Ничего не запускайте из owner-wallet среды.

## Публичная конфигурация без секретов

Сначала изучите `funding_collector/supervised.py:load_config` и `python -m funding_collector live --help` через указанный Python. Создайте свой `native-core-public.json` вне отслеживаемых файлов. Строгий JSON содержит только:

- `version`: целое `1`;
- `lifetime`: целое 1..900 секунд; `poll_seconds`: 0.1..60;
- `legs`: ровно две ноги разных поддерживаемых deployments, primary первой, hedge второй. Для описанного здесь native/Core сценария `symbol` каждой ноги выбирается независимо из свежих metadata без ticker allowlist; экономическое соответствие — ответственность оператора;
- каждая нога: `identity`, `symbol`, публичное `asset_id` (целое или каноническая десятичная строка для широких IDs), строковые положительные `max_quantity`, `max_notional`, `max_slippage_bps` (не выше 100). Extended дополнительно требует явный `fee_rate_cap`;
- `identity`: ровно `deployment`, `account`, `signer`, `api_key_index`. Native: адрес отдельного аккаунта, адрес его dedicated agent, `api_key_index: null`. Core: строковый canonical account index, fingerprint execution key в формате реализации `_binding`, целый зарегистрированный index 4..254. Fingerprint — не сам ключ. Привязка проверяется локально до инициализации SDK. Не вычисляйте fingerprint, передавая ключ в argv/env/чат;
- `intent`: ровно строковые `id`, `quantity`, `side`, `primary_price`, `hedge_price`, `primary_reference`, `hedge_reference`; `side` — `buy` или `sell`, id — 1..80 символов `A-Z a-z 0-9 _ -`.

Не копируйте тестовые account IDs, key fingerprints, цены или market IDs как реальные настройки. Индексы и base units должны соответствовать актуальным публичным metadata обеих площадок. Публичная конфигурация не должна содержать passphrase, private key, owner key, токен, URL или generic signing payload. Она может содержать идентификаторы аккаунтов: не публикуйте её или custody root.

Безопасная локальная проверка схемы (без сети, vault и запуска signer):

```sh
PY=.venv-native-core/bin/python
ROOT="$HOME/.local/share/funding-collector/my-native-core-test"
CFG="$HOME/native-core-public.json"
"$PY" -m funding_collector live check --config "$CFG" --root "$ROOT"
"$PY" -m funding_collector live status --root "$ROOT"
```

Отдельный `live check --public` делает реальные credential-free запросы metadata/книг, но не подтверждает accounts или live-readiness. Это не шаг, выполненный в текущем отчёте.

## Только после review и отдельного разрешения пользователя

Следующие команды — инструкция для личного локального терминала пользователя, **не автоматический acceptance script**. `setup` ничего не регистрирует на бирже: создание/регистрация dedicated execution identities и wallet approvals не автоматизированы и здесь не выполнялись. Owner key никогда не вводится. Протокольные права execution keys могут быть шире локальных ограничений.

```sh
# Локальный зашифрованный vault; секреты только в masked terminal prompts.
"$PY" -m funding_collector live setup --config "$CFG" --root "$ROOT"
# Реальные приватные чтения: запускать только при отдельно разрешённом account-check.
"$PY" -m funding_collector live account-check --config "$CFG" --root "$ROOT"
```

`account-check` запрашивает masked passphrase обеих ног, не просит ARM, не создаёт Worker и не предоставляет submission RPC. Читает registration/positions/open orders; в выводе только boolean registration и счётчики строк позиций/открытых ордеров (не balances и не подтверждение flat). Signer-процессы закрываются после чтений и при ошибке. Локальные SDK journals/public artifacts могут создаваться, worker ledger — нет. Identity binding, bounded lifetime и child RPC allowlist сохраняются. Read-only — локальная capability, а не отзыв протокольных прав исходного ключа.

**Не запускайте `live unlock` в рамках onboarding/read-only проверки.** Эта отдельная команда даёт реальную торговую authority после свежего терминального `ARM EXACT SCOPED ORDERS` на каждом запуске; unattended автозапуск запрещён. Полный primary post-only LIMIT → один opposite protected IOC; ACK не fill. После неоднозначного submit — только reconciliation, не слепой повтор. SIGKILL до ACK проверен на durable fake exchange, но не на реальной бирже. Нельзя менять root/intent, удалять ledger или вручную повторять ордер ради обхода UNKNOWN.

Expiry, Ctrl+C или завершение команды **не отменяют ордера и не закрывают позиции**. Автоматические cancel/flatten/rebalance и гарантированное снятие остаточной экспозиции не реализованы. Используйте отдельный private root 0700; vault/ledger/конфигурацию не коммитьте, не отправляйте в чат и не архивируйте вместе с кодом. Same-user/root compromise, native/SDK dependencies и гарантированное стирание Python immutable secret copies вне модели защиты.
