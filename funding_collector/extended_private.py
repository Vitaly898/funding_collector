"""Explicit-only Extended private adapters; never env-loaded or HTTP-registered.

ExtendedPrivate is the concrete bounded worker path: official SDK models/signing,
explicit credential bundle, account binding and durable ledger. ExtendedSDKBridge
is retained only for existing local compatibility tests; it is NOT a worker/live
registration path and must not be selected by the signer factory.
"""
import asyncio
from dataclasses import dataclass, field
import re
from .domain import Reject, number
from .extended import normalize_market


@dataclass(frozen=True, repr=False)
class ExtendedCredentials:
    """Explicit encrypted-bundle payload; not an Ethereum owner wallet.

    No environment lookup, account registration, HTTP or SDK account construction.
    Public-key derivation is local and verifies bundle consistency.
    """
    api_key: str = field(repr=False)
    stark_private_key: str = field(repr=False)
    stark_public_key: str = field(repr=False)
    vault: int = field(repr=False)

    def __repr__(self):
        return 'ExtendedCredentials(<redacted>)'

    def __post_init__(self):
        if (not isinstance(self.api_key, str) or not 1 <= len(self.api_key) <= 1024
                or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in self.api_key)
                or type(self.vault) is not int or not 0 <= self.vault < 2**64
                or not all(isinstance(k, str) and re.fullmatch(r'0x[0-9a-fA-F]{1,64}', k)
                           for k in (self.stark_private_key, self.stark_public_key))):
            raise Reject('invalid Extended credential bundle')
        from fast_stark_crypto import get_public_key
        try:
            valid = int(self.stark_private_key, 16) > 0 and get_public_key(int(self.stark_private_key, 16)) == int(self.stark_public_key, 16)
        except Exception:
            valid = False
        if not valid:
            raise Reject('Extended Stark keypair mismatch')


class ExtendedSDKBridge:
    @classmethod
    def initialize(cls, sdk_account, *, instrument_map, enable_reads=False, enable_trading=False):
        """Explicit local construction only; injected SDK account, never raw keys/env."""
        from x10.clients.rest import RestApiClient
        from x10.config import MAINNET_CONFIG
        return cls(RestApiClient(MAINNET_CONFIG, sdk_account), instrument_map=instrument_map,
                   enable_reads=enable_reads, enable_trading=enable_trading)

    def __init__(self, client, *, instrument_map, enable_reads=False, enable_trading=False):
        if type(enable_reads) is not bool or type(enable_trading) is not bool:
            raise Reject('local opt-ins must be booleans, not authority objects')
        self.client = client
        self.instrument_map = dict(instrument_map)
        self.reads_enabled = enable_reads
        self.trading_enabled = enable_trading

    def _require(self, trading=False):
        if not (self.trading_enabled if trading else self.reads_enabled):
            raise Reject('Extended private ' + ('trading' if trading else 'reads') + ' disabled')

    def _market(self, instrument):
        # Original DN instrument_map semantics: exact logical symbol, never suffix guessing.
        if instrument not in self.instrument_map:
            raise Reject('instrument not in explicit Extended instrument_map')
        return self.instrument_map[instrument]

    async def _call(self, awaitable):
        return await asyncio.wait_for(awaitable, timeout=15)

    async def close(self):
        await self._call(self.client.close())

    async def reconcile(self, instrument):
        self._require()
        name = self._market(instrument)
        positions = await self._call(self.client.account.get_positions(market_names=[name]))
        orders = await self._call(self.client.account.get_open_orders(market_names=[name]))
        if positions.data is None or orders.data is None:
            raise Reject('missing account data; not an empty account')
        return dict(positions=positions.data, working_orders=orders.data, fills_reconciled=False)

    async def order_status(self, instrument, *, external_id):
        self._require()
        name = self._market(instrument)
        response = await self._call(self.client.account.get_order_by_external_id(external_id=external_id))
        if response.data is None:
            raise Reject('missing order-status response; outcome UNKNOWN')
        if not response.data:
            return dict(status='UNKNOWN', confirmed_fill_base=None, complete=False)
        if len(response.data) != 1:
            raise Reject('ambiguous external_id; no automatic retry')
        order = response.data[0]
        if order.market != name or order.external_id != external_id:
            raise Reject('order-status identity mismatch')
        qty = number(order.qty, True)
        filled = None if order.filled_qty is None else number(order.filled_qty)
        if filled is not None and not 0 <= filled <= qty:
            raise Reject('invalid exchange cumulative fill')
        if str(order.status) == 'FILLED' and filled != qty:
            raise Reject('FILLED status disagrees with cumulative fill')
        return dict(order_id=order.id, external_id=external_id, status=str(order.status),
                    quantity_base=qty, confirmed_fill_base=filled,
                    complete=str(order.status) == 'FILLED' and filled == qty,
                    source='exchange order filledQty; individual trade/fee reconciliation not performed')

    async def order(self, instrument, side, quantity, price, *, external_id, fee_rate,
                    order_type='LIMIT', post_only=True, reduce_only=False):
        self._require(trading=True)
        if (side not in ('buy', 'sell') or order_type not in ('LIMIT', 'MARKET')
                or type(post_only) is not bool or type(reduce_only) is not bool
                or (order_type == 'MARKET' and post_only)
                or not isinstance(external_id, str) or not 1 <= len(external_id) <= 80):
            raise Reject('invalid Extended order parameters')
        qty, protection_price, fee = number(quantity, True), number(price, True), number(fee_rate)
        if fee < 0 or fee > 1:
            raise Reject('invalid explicit fee rate')
        name = self._market(instrument)
        markets = await self._call(self.client.info.get_markets_dict())
        if name not in markets:
            raise Reject('Extended market missing')
        raw = markets[name].model_dump(by_alias=True, mode='json')
        # SDK MarketModel does not retain API status; require active & non-RFQ,
        # and explicitly reject any status retained by a future SDK.
        raw.setdefault('status', 'ACTIVE' if raw.get('active') is True else 'INACTIVE')
        market = normalize_market(raw)
        market.validate(qty, protection_price)  # reject rounding BEFORE SDK signs
        from x10.models.order import OrderSide, OrderType, TimeInForce
        response = await self._call(self.client.place_order(market_name=name,
            amount_of_synthetic=qty, price=protection_price, side=OrderSide(side.upper()),
            taker_fee=fee, order_type=OrderType(order_type), post_only=post_only,
            reduce_only=reduce_only, time_in_force=TimeInForce.IOC if order_type == 'MARKET' else TimeInForce.GTT,
            external_id=external_id))
        if response.data is None:
            raise Reject('order outcome UNKNOWN; reconcile external_id, never blind retry')
        return dict(order_id=response.data.id, external_id=response.data.external_id,
                    status='ACK_ONLY', confirmed_fill_base=None)


class ExtendedPrivate:
    """Worker adapter; explicit register() verifies an existing account, never onboards.

    Caller owns the injected aiohttp-compatible session and its lifecycle. Only
    fixed mainnet read/order routes are reachable. Official SDK signs locally;
    SDK HTTP helpers are deliberately avoided because they log error payloads.
    """
    DEPLOYMENT = 'extended-mainnet-starknet'

    def __init__(self, credentials, identity, raw_market, authority, journal, *,
                 session, fee_rate, timeout_seconds=10, max_pages=20):
        from .live import OrderLedger
        from .extended import BASE_URL
        authority.require_read(identity)
        if (not isinstance(credentials, ExtendedCredentials)
                or identity.deployment != self.DEPLOYMENT
                or not re.fullmatch(r'0|[1-9][0-9]*', identity.account)
                or identity.signer != credentials.stark_public_key
                or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 30
                or type(max_pages) is not int or not 1 <= max_pages <= 100):
            raise Reject('Extended deployment/identity/timeout mismatch')
        self.instrument = normalize_market(raw_market)
        self.market = normalize_market(raw_market)
        try:
            asset_id = int(raw_market['l2Config']['syntheticId'], 16)
        except (KeyError, ValueError, TypeError):
            raise Reject('Extended explicit settlement asset id required') from None
        import json
        self._settlement_binding = json.dumps(raw_market['l2Config'], sort_keys=True)
        self.market.symbol, self.market.market_id = self.market.base_asset, asset_id
        if (authority.symbol, authority.asset_id) != (self.market.symbol, asset_id):
            raise Reject('Extended authority market identity mismatch')
        self.credentials, self.identity, self.authority = credentials, identity, authority
        self.session, self.base_url = session, BASE_URL
        self.fee_rate = number(fee_rate)
        if not 0 <= self.fee_rate <= 1:
            raise Reject('invalid explicit fee cap')
        self.timeout, self.max_pages = timeout_seconds, max_pages
        self.ready = False
        self.ledger = OrderLedger(journal, identity)

    async def _request(self, method, route, model, *, params=None, payload=None):
        import json
        from aiohttp import ClientTimeout
        from x10.models.http import WrappedApiResponseModel
        allowed = {'user/account/info', 'user/positions', 'user/orders', 'user/trades',
                   'user/fees', 'info/markets'}
        if not ((method == 'GET' and (route in allowed or re.fullmatch(
                r'user/orders/external/0x[0-9a-f]{32}', route)))
                or (method == 'POST' and route == 'user/order')):
            raise Reject('Extended route not permitted')
        try:
            async with asyncio.timeout(self.timeout):
                async with self.session.request(method, self.base_url + '/' + route,
                        headers={'X-Api-Key': self.credentials.api_key, 'Accept':'application/json'},
                        params=params, json=payload, allow_redirects=False,
                        timeout=ClientTimeout(total=self.timeout)) as response:
                    if response.status not in (200, 201):
                        raise Reject('Extended HTTP request failed; outcome not inferred')
                    chunks, size = [], 0
                    while True:
                        chunk = await response.content.read(min(65536, 2_000_001-size))
                        if not chunk: break
                        size += len(chunk)
                        if size > 2_000_000: raise Reject('Extended response too large')
                        chunks.append(chunk)
                    raw = json.loads(b''.join(chunks))
                    if (not isinstance(raw, dict) or raw.get('status') != 'OK'
                            or raw.get('error') is not None or raw.get('data') is None):
                        raise Reject('Extended missing or failed response')
                    # Fee currency is implicit quote collateral in SDK 2.6.0.
                    # Do not silently discard a contrary future/raw currency field.
                    if route == 'user/trades':
                        for row in raw['data']:
                            if any(row.get(k, 'USD') != 'USD' for k in ('feeCurrency','feeAsset')):
                                raise Reject('Extended fill fee currency mismatch')
                    return WrappedApiResponseModel[model].model_validate(raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Discard SDK/pydantic/HTTP exceptions: they can contain secrets/body.
            raise Reject('Extended request/schema failed; no payload retained') from None

    async def _verify_account(self):
        from x10.models.account import AccountModel
        self.authority.require_read(self.identity)
        row = (await self._request('GET', 'user/account/info', AccountModel)).data
        try:
            valid = (str(row.id) == self.identity.account and row.status == 'ACTIVE'
                     and int(row.l2_key, 16) == int(self.credentials.stark_public_key, 16)
                     and row.l2_vault == self.credentials.vault)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            self.ready = False
            raise Reject('Extended account identity mismatch')

    async def register(self):
        """Read-only scope binding, not account creation or key registration."""
        self.ready = False
        await self._verify_account()
        self.ready = True

    async def _guard(self):
        if not self.ready: raise Reject('Extended register required')
        await self._verify_account()

    def _row_scope(self, row):
        if str(row.account_id) != self.identity.account or row.market != self.instrument.symbol:
            raise Reject('Extended row account/market identity mismatch')

    async def positions(self):
        from x10.models.position import PositionModel
        await self._guard()
        response = await self._request('GET', 'user/positions', list[PositionModel])
        if response.pagination is not None and response.pagination.cursor is not None:
            raise Reject('Extended positions truncated')
        out = []
        for row in response.data:
            self._row_scope(row)
            if str(row.status) != 'OPENED' or str(row.side) not in ('LONG','SHORT'):
                raise Reject('Extended position status/side mismatch')
            qty = number(row.size, True) * (-1 if str(row.side) == 'SHORT' else 1)
            out.append(dict(symbol=self.market.base_asset, quantity=str(qty)))
        return out

    def _validate_order_row(self, row, client_id=None):
        import json
        self._row_scope(row)
        if client_id is not None and row.external_id != client_id:
            raise Reject('Extended client id mismatch')
        qty = number(row.qty, True)
        filled = None if row.filled_qty is None else number(row.filled_qty)
        if filled is not None and not 0 <= filled <= qty:
            raise Reject('Extended cumulative fill outside order quantity')
        if str(row.status) == 'FILLED' and filled != qty:
            raise Reject('Extended FILLED status/cumulative quantity mismatch')
        known = self.ledger.db.execute('SELECT request FROM orders WHERE id=?', (row.external_id,)).fetchone()
        if known:
            intent = json.loads(known[0])
            if (number(intent['quantity']) != qty or number(intent['price']) != row.price
                    or intent['side'].upper() != str(row.side) or intent['kind'] != str(row.type)
                    or row.reduce_only or row.post_only != (intent['kind'] == 'LIMIT')
                    or str(row.time_in_force) != ('GTT' if intent['kind'] == 'LIMIT' else 'IOC')):
                raise Reject('Extended recovered order differs from durable intent')
        return filled

    async def order_status(self, client_id):
        from x10.models.order import OpenOrderModel
        if not isinstance(client_id, str) or not re.fullmatch(r'0x[0-9a-f]{32}', client_id):
            raise Reject('invalid Extended client id')
        await self._guard()
        response = await self._request('GET', 'user/orders/external/' + client_id, list[OpenOrderModel])
        if not response.data: return dict(status='UNKNOWN', filled_base=None)
        if len(response.data) != 1: raise Reject('ambiguous Extended external id')
        row = response.data[0]
        filled = self._validate_order_row(row, client_id)
        if str(row.status) != 'UNKNOWN':
            self.ledger.status(client_id, str(row.status))
        # Cumulative order quantity is diagnostic only. Worker uses fills().
        return dict(status=str(row.status), order_id=row.id, filled_base=None,
                    cumulative_base=None if filled is None else str(filled))

    async def open_orders(self):
        from x10.models.order import OpenOrderModel
        await self._guard()
        response = await self._request('GET', 'user/orders', list[OpenOrderModel])
        if response.pagination is not None and response.pagination.cursor is not None:
            raise Reject('Extended open orders truncated')
        out = []
        for row in response.data:
            self._validate_order_row(row)
            if str(row.status) not in ('NEW','PARTIALLY_FILLED','UNTRIGGERED'):
                raise Reject('Extended unexpected open order status')
            out.append(dict(order_id=row.id, client_id=row.external_id, symbol=self.market.symbol,
                            quantity=str(row.qty), status=str(row.status)))
        return out

    async def fills(self, order_id):
        from x10.models.trade import AccountTradeModel
        if type(order_id) is not int or order_id < 0:
            raise Reject('Extended numeric order id required')
        await self._guard()
        out, seen, cursors, cursor = [], set(), set(), None
        # Endpoint has no order-id filter: scan all bounded pages, never assume
        # first page means complete. Missing/repeating cursors fail closed.
        for _ in range(self.max_pages):
            params = {'market': self.instrument.symbol, 'limit':100}
            if cursor is not None: params['cursor'] = cursor
            response = await self._request('GET', 'user/trades', list[AccountTradeModel], params=params)
            for row in response.data:
                self._row_scope(row)
                if row.id in seen: raise Reject('duplicate Extended trade id')
                seen.add(row.id)
                if row.order_id != order_id: continue
                if str(row.trade_type) != 'TRADE': raise Reject('Extended non-trade fill')
                qty, price, fee = number(row.qty, True), number(row.price, True), number(row.fee)
                if number(row.value, True) != qty * price:
                    raise Reject('Extended fill notional mismatch')
                out.append(dict(id=str(row.id), quantity=str(qty), price=str(price), fee=str(fee),
                                fee_currency=self.instrument.quote_asset))
            page = response.pagination
            next_cursor = None if page is None else page.cursor
            if next_cursor is None:
                if len(response.data) >= 100:
                    raise Reject('Extended full trade page lacks continuation proof')
                return out
            if next_cursor in cursors: raise Reject('Extended trade pagination repeated')
            cursors.add(next_cursor)
            cursor = next_cursor
        raise Reject('Extended trade history page bound exceeded; incomplete fills')

    async def submit(self, order):
        from datetime import datetime, timedelta, timezone
        import time
        import json
        from x10.config import MAINNET_CONFIG
        from x10.core.stark_account import StarkPerpetualAccount
        from x10.models.market import MarketModel
        from x10.models.fee import TradingFeeModel
        from x10.models.order import OrderSide, OrderType, TimeInForce, PlacedOrderModel
        from x10.signing.order_object import create_order_object
        self.authority.check(self.identity, self.market.symbol, self.market.market_id, order)
        self.market.validate(order.quantity, order.price)
        await self._guard()
        response = await self._request('GET', 'info/markets', list[dict])
        rows = [r for r in response.data if r.get('name') == self.instrument.symbol]
        if len(rows) != 1: raise Reject('Extended market missing/ambiguous')
        fresh = normalize_market(rows[0])
        fresh.validate(order.quantity, order.price)
        if json.dumps(rows[0].get('l2Config'), sort_keys=True) != self._settlement_binding:
            raise Reject('Extended settlement metadata changed')
        try:
            sdk_market = MarketModel.model_validate(rows[0])
            base = order.quantity * sdk_market.l2_config.synthetic_resolution
            quote = order.quantity * order.price * sdk_market.l2_config.collateral_resolution
            if (base != base.to_integral_value() or quote != quote.to_integral_value()
                    or sdk_market.l2_config.synthetic_resolution <= 0
                    or sdk_market.l2_config.collateral_resolution <= 0):
                raise ValueError('unrepresentable settlement units')
        except Exception:
            raise Reject('Extended exact settlement precision required') from None
        fees = (await self._request('GET', 'user/fees', list[TradingFeeModel],
                                   params={'market':self.instrument.symbol})).data
        if (len(fees) != 1 or fees[0].market != self.instrument.symbol
                or not 0 <= number(fees[0].taker_fee_rate) <= self.fee_rate
                or number(fees[0].builder_fee_rate) != 0):
            raise Reject('Extended fee scope/cap mismatch')
        # Fetches may have consumed the authority lifetime. Recheck before WAL.
        self.authority.check(self.identity, self.market.symbol, self.market.market_id, order)
        # SDK's nonce generator uses uint32. A durable monotone counter seeded
        # from seconds preserves that range; never use SDK random nonce or ns.
        nonce = self.ledger.reserve(order, int(time.time()), monotonic=True)
        try:
            if nonce >= 2**32: raise ValueError('nonce range exhausted')
            account = StarkPerpetualAccount(vault=self.credentials.vault,
                private_key=self.credentials.stark_private_key, public_key=self.credentials.stark_public_key,
                api_key=self.credentials.api_key)
            built = create_order_object(account=account, market=sdk_market,
                amount_of_synthetic=order.quantity, price=order.price, side=OrderSide(order.side.upper()),
                starknet_domain=MAINNET_CONFIG.signing.starknet_domain,
                order_type=OrderType(order.kind), post_only=order.kind == 'LIMIT',
                time_in_force=TimeInForce.GTT if order.kind == 'LIMIT' else TimeInForce.IOC,
                expire_time=datetime.now(timezone.utc) + timedelta(seconds=60),
                order_external_id=order.client_id, nonce=nonce, taker_fee=self.fee_rate,
                reduce_only=False, builder_id=None, builder_fee=None)
            expected_base = base if order.side == 'buy' else -base
            expected_quote = -quote if order.side == 'buy' else quote
            if (built.qty != order.quantity or built.price != order.price or built.id != order.client_id
                    or built.nonce != nonce or built.debugging_amounts is None
                    or built.debugging_amounts.synthetic_amount != expected_base
                    or built.debugging_amounts.collateral_amount != expected_quote):
                raise ValueError('SDK changed exact units')
            payload = built.to_api_request_json(exclude_none=True)
        except Exception:
            self.ledger.status(order.client_id, 'REJECTED')
            raise Reject('Extended local SDK build/sign failed before send') from None
        try:
            response = await self._request('POST', 'user/order', PlacedOrderModel, payload=payload)
            ack = response.data
            if ack.external_id != order.client_id or ack.id < 0:
                raise Reject('Extended ack identity mismatch')
            self.ledger.status(order.client_id, 'ACK_ONLY')
            return dict(status='ACK_ONLY', order_id=ack.id, filled_base=None)
        except asyncio.CancelledError:
            self.ledger.status(order.client_id, 'UNKNOWN')
            raise
        except Exception:
            self.ledger.status(order.client_id, 'UNKNOWN')
            return dict(status='UNKNOWN', filled_base=None)

    def close(self):
        """Worker-compatible sync close. Session ownership stays with caller."""
        self.ready = False
        self.ledger.close()
