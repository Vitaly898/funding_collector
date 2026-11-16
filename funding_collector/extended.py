"""Extended Starknet public contracts. No account, SDK import or signing.

Units follow pinned official MarketModel / create_order_object:
amount_of_synthetic is assetName base; price is collateralAssetName per base.
Only continuous non-RFQ USD perpetuals are admitted.
"""
from .domain import Instrument, Reject, number

SDK_REVISION = 'da9a79726d23fb605518348dd6f4fa7d3dbd83c9'
BASE_URL = 'https://api.starknet.extended.exchange/api/v1'


def normalize_market(raw):
    if (raw.get('type') != 'PERPETUAL' or raw.get('active') is not True
            or raw.get('status') != 'ACTIVE' or raw.get('isRfq') is not False
            or raw.get('isOffHours') is not False or raw.get('tradingHours') != 'CONTINUOUS'
            or raw.get('collateralAssetName') != 'USD'
            or raw.get('name') != raw.get('assetName', '') + '-USD'):
        raise Reject('Extended supports only active continuous non-RFQ USD perpetuals')
    tc = raw['tradingConfig']
    return Instrument(raw['name'], raw['name'], raw['assetName'], 'USD',
                      'linear_perpetual', 'base', '1', True,
                      tc['minOrderSizeChange'], tc['minPriceChange'], tc['minOrderSize'], '0',
                      str(min(number(tc['maxMarketOrderValue'], True), number(tc['maxLimitOrderValue'], True))), raw)


from .public import PublicClient
from pathlib import Path
from types import SimpleNamespace
import re
import time
from .domain import Book


class ExtendedPublicClient(PublicClient):
    def __init__(self, artifacts):
        self.deployment = SimpleNamespace(name='extended', base_url=BASE_URL, chain_id=None)
        self.artifacts = Path(artifacts)
        self.artifacts.mkdir(parents=True, exist_ok=True)

    def success(self, raw):
        return isinstance(raw, dict) and raw.get('status') == 'OK' and 'data' in raw

    def valid_route(self, route, params):
        return not params and (route == 'info/markets' or re.fullmatch(
            r'info/markets/[A-Za-z0-9_.-]{1,40}/orderbook', route) is not None)


class ExtendedPublicAdapter:
    def __init__(self, artifacts):
        self.client = ExtendedPublicClient(artifacts)

    def snapshot(self, symbol):
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,40}', symbol):
            raise Reject('invalid Extended exact market name')
        raw, _ = self.client.get('info/markets')
        matches = [m for m in raw['data'] if m['name'] == symbol]
        if len(matches) != 1:
            raise Reject('Extended exact market not found: ' + symbol)
        market = normalize_market(matches[0])  # reject RFQ BEFORE fetching indicative book
        response, at = self.client.get('info/markets/' + symbol + '/orderbook')
        book = response['data']
        if book['market'] != symbol:
            raise Reject('Extended book instrument mismatch')
        normalized = {out: [dict(price=o['price'], remaining_base_amount=o['qty']) for o in book[key]]
                      for out, key in [('bids', 'bid'), ('asks', 'ask')]}
        return market, Book(normalized, at), dict(base_url=BASE_URL,
            book_observed_at_unix_s=at, book_age_seconds_at_preview=time.time() - at,
            exchange_event_timestamp=None, sdk_revision=SDK_REVISION,
            freshness_basis='local receipt / conservative HTTP Date and Age; REST book has no event timestamp',
            public_status='public connected / private not configured',
            minimum_notional='not specified in public tradingConfig; minOrderSize enforced')
