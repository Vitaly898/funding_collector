"""EVEDEX production public data via optional official Node SDK; never keys.

Only reviewed unit BTC/ETH perpetuals enter pairing. All other catalog entries
are visible but refused; no ticker inference, lot or multiplier conversion.
"""
from email.utils import parsedate_to_datetime
import json
import math
from pathlib import Path
import subprocess
import time

from .domain import Book, Instrument, Reject, number

BRIDGE = Path(__file__).resolve().parent.parent / 'optional' / 'evedex' / 'bridge.cjs'
REVIEWED = {'ETHUSD': ('eth-usd', 'eth', 'ETH'), 'BTCUSD': ('btc-usd', 'btc', 'BTC')}
ORIGIN = 'https://trading-api.evedex.com'


class EvedexInstrument(Instrument):
    def validate(self, qty, price):
        super().validate(qty, price)
        if (number(qty) > number(self.raw['maxQuantity'], True)
                or not number(self.raw['minPrice'], True) <= number(price) <= number(self.raw['maxPrice'], True)):
            raise Reject('EVEDEX quantity/price outside market bounds')


def normalize_market(raw):
    identity = REVIEWED.get(raw.get('name'))
    if (identity is None or (raw.get('id'), raw.get('from', {}).get('id'), raw.get('from', {}).get('symbol')) != identity
            or raw.get('to', {}).get('id') != 'usd' or raw.get('to', {}).get('symbol') != 'USD'
            or raw.get('type') != 'perpetual-futures' or number(raw.get('multiplier', 0)) != 1
            or number(raw.get('lotSize', 0)) != 1 or raw.get('trading') != 'all'
            or raw.get('marketState') != 'OPEN' or raw.get('matcherState') != 'active'
            or raw.get('label', {}).get('orderBookIndicative') is not False):
        raise Reject('EVEDEX unsupported/unverified identity, multiplier, RFQ/indicative or closed market')
    return EvedexInstrument(raw['name'], raw['id'], identity[2], 'USD', 'linear_perpetual',
        'base', '1', True, raw['quantityIncrement'], raw['priceIncrement'],
        raw['minQuantity'], raw['minVolume'], raw=raw)


def normalize_book(raw, receipt_at):
    event = raw.get('t')
    if type(event) is not int or event <= 0 or event / 1000 > receipt_at + 5:
        raise Reject('EVEDEX book exchange timestamp invalid/future')
    if any(not isinstance(raw.get(side), list) or not 1 <= len(raw[side]) <= 100 for side in ('asks', 'bids')):
        raise Reject('EVEDEX book depth outside bounds')
    return Book({side: [dict(price=r['price'], remaining_base_amount=r['quantity'])
                       for r in raw[side]] for side in ('asks', 'bids')}, min(receipt_at, event / 1000))


def validate_pair(entries):
    own = [(c, m) for c, m in entries if c.deployment.venue == 'evedex']
    other = [(c, m) for c, m in entries if c.deployment.venue != 'evedex']
    if len(own) != 1 or len(other) != 1:
        raise Reject('EVEDEX requires independently verified countervenue')
    c, m = own[0]; oc, om = other[0]
    if (c.deployment.id != 'production-161803' or not isinstance(m, EvedexInstrument)
            or oc.deployment.venue != 'lighter' or oc.deployment.id not in ('core', 'rh')
            or om.symbol != m.base_asset or om.base_asset != m.base_asset
            or om.multiplier != 1 or not om.units_verified or om.quote_asset != 'USD'):
        raise Reject('EVEDEX pair not verified: exact BTC/ETH Core/RH base exposure required')


class EvedexPublicAdapter:
    def __init__(self, artifacts):
        self.artifacts = Path(artifacts)

    def read(self, op, symbol=None):
        payload = dict(op=op, artifacts=str(self.artifacts.resolve()))
        if symbol is not None:
            payload['symbol'] = symbol
        try:
            result = subprocess.run(['node', str(BRIDGE)], input=json.dumps(payload),
                text=True, capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise Reject('EVEDEX optional Node bridge unavailable/deadline exceeded') from error
        if result.returncode or len(result.stdout) > 6 * 1024 * 1024:
            raise Reject('EVEDEX SDK public unavailable; install optional/evedex with npm ci --ignore-scripts; inspect raw artifacts')
        try:
            data = json.loads(result.stdout)
            if data['environment'] != 'production' or data['exchange_uri'] != ORIGIN or data['trading_enabled'] is not False:
                raise Reject('wrong EVEDEX environment')
            return data
        except (ValueError, KeyError, TypeError) as error:
            raise Reject('EVEDEX bridge invalid response') from error

    @staticmethod
    def observed(records):
        times = []
        for record in records:
            at = float(record['requested_at_unix_s'])
            age = float(record['headers'].get('age', 0))
            if not math.isfinite(age) or age < 0:
                raise Reject('invalid EVEDEX cache age')
            at -= age
            if record['headers'].get('date'):
                at = min(at, parsedate_to_datetime(record['headers']['date']).timestamp())
            if not math.isfinite(at) or record['status'] != 200:
                raise Reject('invalid EVEDEX receipt/status')
            times.append(at)
        if not times:
            raise Reject('missing EVEDEX provenance')
        return min(times)

    def catalog(self):
        data = self.read('catalog')
        rows = []
        for raw in data['instruments']:
            try:
                normalize_market(raw)
                status = 'REVIEWED_UNITS_COUNTERVENUE_REQUIRED'
            except Reject:
                status = 'REFUSED_UNVERIFIED_OR_UNSUPPORTED'
            rows.append(dict(symbol=raw['name'], pairing_status=status, raw=raw))
        return dict(venue='evedex', environment='production', total=len(rows), markets=rows,
            observed_at_unix_s=self.observed(data['records']), trading_enabled=False,
            api_key_account='NOT_CONFIGURED_READ_ONLY', one_click_signer='NOT_CONFIGURED',
            live_hedge_blocker='SDK MARKET uses cashQuantity, not exact base quantity; no protected base IOC in inspected contract')

    def snapshot(self, symbol):
        if symbol not in REVIEWED:
            raise Reject('EVEDEX exact reviewed BTCUSD/ETHUSD required; RFQ/unknown multiplier refused')
        data = self.read('snapshot', symbol)
        rows = [r for r in data['instruments'] if r['name'] == symbol]
        if len(rows) != 1:
            raise Reject('EVEDEX exact market missing/ambiguous')
        market = normalize_market(rows[0])
        book = normalize_book(data['book'], self.observed(data['records']))
        return market, book, dict(base_url=ORIGIN, chain_id=161803,
            book_observed_at_unix_s=book.observed_at, book_age_seconds_at_preview=time.time()-book.observed_at,
            exchange_event_timestamp=data['book']['t'], freshness_basis='min exchange book t / request start / HTTP Date and Age',
            quantity_unit='base', multiplier='1', price_unit='USD quote per BTC/ETH; collateral token not equated with bank USD',
            fees_public_not_account_verified=market.raw.get('fees'), fees='explicit user bps required; public defaults are not account tier',
            public_status='public connected / private not configured / live execution disabled',
            market_execution='PAPER ONLY: SDK MARKET cashQuantity cannot guarantee equal base hedge',
            depth_round_price='0.1 SDK query; server levels retained unchanged; bounded 100 levels')
