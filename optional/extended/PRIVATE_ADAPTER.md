# Extended private adapter integration contract

Status: concrete adapter implemented and offline tested; NOT wired into factory,
keystore, HTTP, session or worker registration. No live connectivity/account
verification claimed. Keep `ExtendedSDKBridge` unregistered: that old compatibility
bridge has no durable worker guarantees.

## SDK and scope

Use existing `requirements-extended.lock`, including:

- `x10-python-trading-starknet==2.6.0`, official git revision
  `da9a79726d23fb605518348dd6f4fa7d3dbd83c9`.
- `fast-stark-crypto==0.5.0`, `aiohttp==3.14.5`, SDK's pinned dependencies.
- Verified interpreter: `/opt/data/cache/scratch/funding-collector-extended/venv/bin/python`.

No third-party source is vendored/copied. Actual installed official source was
read. Imports used in the concrete path:

- `x10.config.MAINNET_CONFIG` (SN_MAIN/Perpetuals/v0/revision 1).
- `x10.core.stark_account.StarkPerpetualAccount` (local object, only at submit).
- `x10.signing.order_object.create_order_object` (explicit durable nonce).
- `x10.models.http.WrappedApiResponseModel`.
- `x10.models.account.AccountModel`, `position.PositionModel`,
  `order.OpenOrderModel`, `order.PlacedOrderModel`, `trade.AccountTradeModel`,
  `fee.TradingFeeModel`, `market.MarketModel`, order enums.

Do NOT use `RestApiClient.place_order`: it cannot take an explicit nonce. Do NOT
use SDK HTTP utilities here: errors can log response payloads, and SDK default
request timeout is 500 seconds. Adapter uses its own bounded no-log HTTP and
SDK models/signing. Mainnet host is fixed; redirects disabled; response bounded
at 2 MB; every request has both aiohttp and coroutine deadlines (default 10s,
integer 1..30). No retry middleware is authorized on the injected session.

## Parent wiring required (shared files intentionally untouched)

1. Add deployment `extended-mainnet-starknet` with fixed API base
   `https://api.starknet.extended.exchange/api/v1`, domain SN_MAIN.
2. Identity: `account` is canonical decimal API **account ID**, not accountIndex
   or vault; `signer` is exact hex Stark public key from the bundle, not an EVM
   owner/agent address; `api_key_index=None`. Bind nonce_scope to deployment,
   account, signer and preferably explicit vault. Never reuse a journal between
   these scopes. Never share this signing workload/key with another app.
3. Encrypted typed bundle fields (no environment fallback):
   `api_key: str`, `stark_private_key: str`, `stark_public_key: str`, `vault: int`.
   Import `ExtendedCredentials` from `funding_collector.extended_private`.
   Constructor is immutable/repr-redacted and locally verifies public key against
   private key. Store this bundle as a dedicated credential kind, not owner-key
   text. Vault bool/string is rejected. No credentials belong in task journals.
4. Authority: base symbol `ETH` (or independently reviewed BTC/SOL), `asset_id`
   equals `int(raw_market['l2Config']['syntheticId'], 16)` (NOT an invented small
   exchange market index). The adapter exposes worker `market.symbol='ETH'` and
   numeric `market.market_id` for existing worker Authority checks. Exact exchange
   symbol remains `adapter.instrument.symbol='ETH-USD'`. Public raw metadata must
   include status, full tradingConfig, assetPrecision/collateralAssetPrecision,
   marketStats and l2Config. Do not infer missing status from active.
5. Factory/session create and own an explicit aiohttp-compatible session with
   no retries, no credential/payload tracing and safe TLS configuration; pass it
   as `session`. Adapter never creates a session or performs HTTP in constructor.
   Never use a fake session outside tests or advertise a synthetic connection.
6. Construct `ExtendedPrivate(credentials, identity, raw_market, authority,
   journal, session=session, fee_rate=Decimal(explicit_fee_cap))`. Optional bounded
   `timeout_seconds=10`, `max_pages=20` (1..100). Journal is an exclusive private
   0700 directory. Constructor only validates local scope and opens OrderLedger.
7. Explicit `await adapter.register()` performs read-only verification of an
   **already existing** API account (`id`, ACTIVE status, `l2Key`, `l2Vault`). This
   does NOT onboard, register a key, change leverage, or grant permissions. Every
   public adapter method rechecks this account binding before reading/sending.
8. Await `positions()`, `open_orders()`, `order_status(client_id)`, `fills(order_id)`,
   `submit(typed live.Order)`. `close()` is synchronous and closes the ledger;
   parent must separately `await session.close()` in a finally block and must
   close it after failed registration as well. Runtime arming stays in Authority.
9. Worker MUST admit verified per-venue fee quote: this adapter emits
   `fee_currency='USD'`, not USDC. Existing worker's hardcoded USDC check and
   `fees_usdc` aggregation need deliberate currency-aware handling. Do not relabel
   USD as USDC. SDK AccountTradeModel has no currency field: USD follows the
   admitted USD collateral contract/settlement; any contrary raw feeCurrency or
   feeAsset is rejected. Accounting must preserve that basis explicitly.

## Worker return contracts

- `positions`: `[{symbol: 'ETH', quantity: '<signed exact base>'}]`; unfiltered
  positions endpoint ensures unrelated inventory fails closed instead of hiding.
- `open_orders`: `[{order_id: int, client_id: str, symbol: 'ETH',
  quantity: '<base>', status: str}]`; malformed/foreign/truncated is not empty.
- `order_status`: `{status, order_id, filled_base: None,
  cumulative_base: '<diagnostic cumulative>'|None}`. Empty lookup returns UNKNOWN
  with no order id and never clears the WAL. FILLED must equal order quantity,
  but is NOT an individual-fill proof. Recovery checks stored quantity, price,
  side, type, reduceOnly, postOnly, TIF before clearing an unresolved barrier.
- `fills`: `[{id: str, quantity: '<base>', price: '<USD/base>', fee: '<signed USD>',
  fee_currency: 'USD'}]`. Verified account/market/order, unique trade IDs, finite
  amounts and exact price*qty=value. Only TRADE fills accepted; signed maker
  rebates permitted. Pagination is bounded and rejects duplicate/repeated/missing
  continuation proof. No first-page-only complete fill claim.
- `submit`: ACK_ONLY/order_id/filled_base=None on matching parsed ACK, otherwise
  UNKNOWN/filled_base=None on any post-send timeout, malformed ACK or API error.
  Cancellation records UNKNOWN and re-raises. Never automatically retry UNKNOWN.

LIMIT => official OrderType.LIMIT, postOnly=true, GTT.
MARKET => official OrderType.MARKET, postOnly=false, IOC, explicit protection
price. Both use exact typed base quantity, no float conversion, no quote sizing.
Fresh metadata and fee cap are checked before WAL. Durable shared OrderLedger
reserves client ID and uint32-range monotone nonce before official SDK signing;
nonce is seeded from seconds, never SDK random or nanoseconds. Exact base and
quote settlement amounts are verified before POST. Expiry is explicit 60 seconds;
builder attribution, replacement, reduce-only, transfers and approvals disabled.

## Permissions and limitations

API authentication is `X-Api-Key`; reads need access to account/info, positions,
orders/external-ID and trades, fees, plus public info/markets. Trading additionally
needs Stark private/public key and vault for settlement signature and POST
user/order. There is no permission-discovery endpoint in this adapter and no
claim of exchange-enforced trade-only scope: the Stark key is an account key and
SDK also exposes withdrawal/transfer signing. Use a dedicated separately funded
account and keep restricted credentials isolated. Local Authority does not turn
that Stark key into a scoped venue agent. No permission acquisition is automated.

Trade history exceeding max_pages fails closed. A final full 100-row page with
no cursor is conservatively rejected. This favors incomplete-history blocking
over silently missing fills. Fee currency is implicit SDK collateral basis,
not a token-level USDC identity proof. Real account access, authorization,
exchange rejection shapes, server pagination behavior and order acceptance have
NOT been live-tested and must never be reported as connected. No live/testnet
orders, real keys, approval calls or private reads were used.

## Offline verification / reusable workflow

1. Inspect installed pinned SDK source before changing imports, nonce/signing,
   field aliases or pagination; do not adapt obsolete DN code.
2. Write failing synthetic HTTP tests before each new behavior/fix.
3. Run only offline tests in the isolated venv with socket connect blocked.
4. Exercise real create_order_object and crypto verify on dummy key 1, inspect
   base/quote debugging units, assert WAL already exists before signing.
5. Verify timeout/restart/foreign identity and incomplete histories fail closed.

Command executed:

```
/opt/data/cache/scratch/funding-collector-extended/venv/bin/python -m unittest \
  tests_fc.test_extended_private_worker tests_fc.test_extended_private \
  tests_fc.test_extended -v
```

Result: **27 tests passed, 2.072s** (17 new concrete-adapter cases plus existing
bridge/public contracts). No tests skipped in this venv. Synthetic HTTP case
count is not measured code coverage or live connectivity verification.
