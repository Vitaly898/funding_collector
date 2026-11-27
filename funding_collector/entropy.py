"""Entropy's own HIP-3 io deployment. Public reads only; no SDK or keys.

Exact venue-qualified contract units, never inferred spot/share equivalence.
Economic correspondence across independently selected contracts is operator-owned.
"""
from decimal import Decimal
from email.utils import parsedate_to_datetime
import json
import math
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
import uuid
from .domain import Instrument, Book, Reject, number
from .public import NoRedirect

INFO_URL = 'https://api.hyperliquid.xyz/info'
USDC_TOKEN_ID = '0x6d1e7cde53ba9467b783cb7c530ce054'
EQUITY_SOURCE = 'https://docs.entropy.io/asset-directory/equity-assets'
UNITS_SOURCE = 'https://docs.entropy.io/market-types/equity-perpetuals'
LIGHTER_SOURCE = 'https://docs.lighter.xyz/trading/real-world-assets-rwas/market-specifications'
def contract_identity(coin):
    if not isinstance(coin,str) or not re.fullmatch(r'io:[A-Z0-9_.-]{1,30}',coin):
        raise Reject('exact Entropy io contract required')
    return 'entropy:'+coin


class EntropyPublicClient:
    """Fixed-origin POST /info, exact public body allowlist; never /exchange.

    No persistent cache: each read uses no-cache and conservative HTTP Date/Age.
    Raw public bodies (including HTTP errors) are bounded and retained on disk.
    """
    trading_enabled = False
    MAX_BODY = 2 * 1024 * 1024
    venue = 'entropy'
    dex = 'io'

    def valid_payload(self, payload):
        kind = payload.get('type')
        return ((kind in ('perpDexs', 'spotMeta') and set(payload) == {'type'})
                or (kind == 'metaAndAssetCtxs' and payload == {'type': kind, 'dex': 'io'})
                or (kind == 'l2Book' and set(payload) == {'type', 'coin'}
                    and isinstance(payload['coin'], str)
                    and re.fullmatch(r'io:[A-Z0-9_.-]{1,30}', payload['coin'])))

    def __init__(self, artifacts):
        self.artifacts = Path(artifacts)
        self.artifacts.mkdir(parents=True, exist_ok=True)

    def request(self, *args, **kwargs):
        raise Reject('TRADING_DISABLED: ' + self.venue + ' supports only public info allowlist')

    get = create_order = cancel_order = withdraw = sign = request

    def info(self, payload):
        if not isinstance(payload, dict):
            raise Reject('public info body must be an object')
        kind = payload.get('type')
        valid = self.valid_payload(payload)
        if not valid:
            raise Reject('body not in ' + self.venue + ' public POST /info allowlist')
        started = time.time()
        envelope = dict(venue=self.venue, dex=self.dex, method='POST', url=INFO_URL,
                        request=payload, requested_at_unix_s=started, body=None)
        try:
            req = urllib.request.Request(INFO_URL, data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json', 'Cache-Control': 'no-cache',
                         'User-Agent': 'funding-collector-read-only/0.1'}, method='POST')
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=15) as response:
                body = response.read(self.MAX_BODY + 1)
                envelope.update(status=response.status, headers=dict(response.headers),
                    body=body[:self.MAX_BODY].decode('utf-8', errors='replace'),
                    body_truncated=len(body) > self.MAX_BODY)
                if len(body) > self.MAX_BODY:
                    raise Reject('public response exceeds limit')
                raw = json.loads(body.decode('utf-8'))
                if response.status != 200 or not isinstance(raw, (dict, list)) or (isinstance(raw, dict) and 'error' in raw):
                    raise Reject('public info non-success response')
                age = float(response.headers.get('Age', '0'))
                if not math.isfinite(age) or age < 0:
                    raise Reject('invalid HTTP cache age')
                at = min(started, started - age)
                if response.headers.get('Date'):
                    at = min(at, parsedate_to_datetime(response.headers['Date']).timestamp())
                if not math.isfinite(at):
                    raise Reject('invalid public timestamp')
                envelope['observed_at_unix_s'] = at
                return raw, at
        except urllib.error.HTTPError as error:
            with error:
                body = error.read(self.MAX_BODY + 1)
            envelope.update(status=error.code, headers=dict(error.headers),
                body=body[:self.MAX_BODY].decode('utf-8', errors='replace'),
                body_truncated=len(body) > self.MAX_BODY, error='HTTPError')
            raise Reject(self.venue + ' public HTTP error: ' + str(error.code)) from error
        except (OSError, ValueError, Reject) as error:
            envelope['error'] = type(error).__name__ + ': ' + str(error)
            raise Reject(self.venue + ' public read failed; no synthetic fallback: ' + str(error)) from error
        finally:
            envelope['received_at_unix_s'] = time.time()
            (self.artifacts / (self.venue + '-' + str(kind) + '-' + uuid.uuid4().hex + '.json')).write_text(
                json.dumps(envelope, indent=2), encoding='utf-8')


def resolve_contract(meta_context, dexs, spot, coin):
    identity=contract_identity(coin)
    meta, contexts = meta_context
    indexes = [i for i, d in enumerate(dexs) if d and d.get('name') == 'io']
    if len(indexes) != 1 or indexes[0] < 1:
        raise Reject('Entropy exact io deployment missing or ambiguous')
    rows = meta['universe']
    if len(rows) != len(contexts) or len({m['name'] for m in rows}) != len(rows):
        raise Reject('Entropy metadata/context identity mismatch')
    if any(not m['name'].startswith('io:') for m in rows):
        raise Reject('wrong Entropy deployment namespace')
    tokens = [t for t in spot['tokens'] if t['index'] == meta.get('collateralToken')]
    if (len(tokens) != 1 or tokens[0].get('name') != 'USDC'
            or tokens[0].get('tokenId') != USDC_TOKEN_ID or tokens[0].get('isCanonical') is not True):
        raise Reject('unverified Entropy collateral')
    matches = [(i, m) for i, m in enumerate(rows) if m['name'] == coin]
    if len(matches) != 1:
        raise Reject('Entropy requires exact io:COIN symbol; not found: ' + str(coin))
    index, raw = matches[0]
    decimals = raw['szDecimals']
    if type(decimals) is not int or not 0 <= decimals <= 6:
        raise Reject('unsupported Entropy size precision')
    return dict(coin=coin, asset_id=100000 + indexes[0] * 10000 + index,
                dex='io', dex_index=indexes[0], index_in_meta=index,
                size_step=format(Decimal(10) ** -decimals, 'f'), sz_decimals=decimals,
                collateral_asset='USDC', collateral_token_id=USDC_TOKEN_ID,
                price_unit='USD per '+coin+' contract unit',
                price_currency='USD', multiplier='1',
                underlying_id=identity, units_verified=True,
                is_delisted=raw.get('isDelisted', False), raw=raw, context=contexts[index],
                funding_interval_seconds=3600, funding_unit='decimal fraction per hour; current estimate',
                units_sources=[EQUITY_SOURCE, UNITS_SOURCE])


class EntropyInstrument(Instrument):
    def __init__(self, contract):
        if contract['is_delisted'] or not contract['units_verified']:
            raise Reject('Entropy delisted or unverified underlying/price units; no compatible hedge')
        self.sz_decimals = contract['sz_decimals']
        super().__init__(contract['coin'], contract['asset_id'], contract['coin'].split(':')[1], 'USD',
            'linear_perpetual', 'base', '1', True, contract['size_step'],
            Decimal(10) ** -(6 - self.sz_decimals), contract['size_step'], '10', raw=contract)

    def validate(self, qty, price):
        super().validate(qty, price)
        px = number(price, True).normalize()
        if px != px.to_integral_value() and len(px.as_tuple().digits) > 5:
            raise Reject('Entropy price exceeds 5 significant figures')


def validate_pair(entries):
    """Technical contract units only; never a spot/share equivalence claim."""
    entropy = [(c, m) for c, m in entries if c.deployment.venue == 'entropy']
    others = [(c, m) for c, m in entries if c.deployment.venue != 'entropy']
    if len(entropy) != 1 or len(others) != 1:
        raise Reject('Entropy hedge needs one independently verified countervenue')
    ec, em = entropy[0]
    oc, om = others[0]
    if (ec.deployment.id != 'hyperliquid-mainnet-io' or not isinstance(em, EntropyInstrument)
            or em.raw.get('underlying_id') != contract_identity(em.symbol)
            or not em.raw.get('units_verified') or em.raw.get('is_delisted')
            or (oc.deployment.venue,oc.deployment.id) not in (('lighter','core'),('lighter','rh'),('hyperliquid','hyperliquid-mainnet-native'),('aster','aster-mainnet-v3'),('extended','starknet-mainnet'))
            or om.contract_type!='linear_perpetual' or om.quantity_unit!='base'
            or om.quote_asset != 'USD' or om.multiplier != 1 or not om.units_verified):
        raise Reject('no proven compatible Entropy pair: deployment/underlying/base/price units mismatch')
    if oc.deployment.venue=='lighter' and (number(om.raw.get('multiplier','1'))!=1 or number(om.raw.get('quote_multiplier','1'))!=1):
        raise Reject('unsupported Lighter unit multiplier')


class EntropyPublicAdapter:
    def __init__(self, artifacts):
        self.client = EntropyPublicClient(artifacts)

    def _metadata(self):
        dexs,da=self.client.info({'type':'perpDexs'})
        spot,sa=self.client.info({'type':'spotMeta'})
        meta,ma=self.client.info({'type':'metaAndAssetCtxs','dex':'io'})
        return dexs,spot,meta,min(da,sa,ma)

    def catalog(self):
        dexs, dex_at = self.client.info({'type': 'perpDexs'})
        spot, spot_at = self.client.info({'type': 'spotMeta'})
        meta, meta_at = self.client.info({'type': 'metaAndAssetCtxs', 'dex': 'io'})
        markets = [resolve_contract(meta, dexs, spot, row['name']) for row in meta[0]['universe']]
        for row in markets:
            row['pairing_status'] = ('REFUSED_DELISTED' if row['is_delisted'] else
                'REVIEWED_UNITS_COUNTERVENUE_REQUIRED' if row['units_verified'] else 'REFUSED_UNVERIFIED_UNITS')
        return dict(own_dex='io', venue='entropy', total=len(markets), markets=markets,
            active_count=sum(not m['is_delisted'] for m in markets),
            reviewed_unit_count=sum(m['units_verified'] and not m['is_delisted'] for m in markets),
            observed_at_unix_s=min(dex_at,spot_at,meta_at), trading_enabled=False,
            scope='own io deployment only; NOT every market aggregated by Entropy frontend',
            warning='Reviewed units are not a liquidity/hedge recommendation. Countervenue exact contract and fresh depth required.')

    def snapshot(self, symbol):
        if not isinstance(symbol, str) or not re.fullmatch(r'io:[A-Z0-9_.-]{1,30}', symbol):
            raise Reject('Entropy requires exact io:COIN; no generic Hyperliquid symbol')
        dexs, dex_at = self.client.info({'type': 'perpDexs'})
        spot, spot_at = self.client.info({'type': 'spotMeta'})
        meta, meta_at = self.client.info({'type': 'metaAndAssetCtxs', 'dex': 'io'})
        contract = resolve_contract(meta, dexs, spot, symbol)
        market = EntropyInstrument(contract)  # Fail before book if units unknown.
        raw, receipt_at = self.client.info({'type': 'l2Book', 'coin': symbol})
        if raw.get('coin') != symbol:
            raise Reject('Entropy book coin mismatch')
        event = raw.get('time')
        if type(event) is not int or event <= 0 or event / 1000 > receipt_at + 5:
            raise Reject('Entropy book event timestamp invalid/future')
        at = min(receipt_at, event / 1000, dex_at, spot_at, meta_at)
        levels = raw['levels']
        if len(levels) != 2:
            raise Reject('Entropy book requires bids and asks')
        normalized = {key: [dict(price=r['px'], remaining_base_amount=r['sz']) for r in rows]
                      for key, rows in zip(('bids', 'asks'), levels)}
        return market, Book(normalized, at), dict(
            base_url=INFO_URL, dex='io', asset_id=contract['asset_id'],
            book_observed_at_unix_s=at, book_age_seconds_at_preview=time.time() - at,
            exchange_event_timestamp=event, collateral_asset='USDC', collateral_token_id=USDC_TOKEN_ID,
            price_unit=contract['price_unit'], underlying_id=contract['underlying_id'],
            multiplier='1', units_sources=contract['units_sources'],
            context=contract['context'], funding_interval_seconds=3600,
            funding_unit=contract['funding_unit'], margin=contract['raw'].get('marginMode',
                'isolated' if contract['raw'].get('onlyIsolated') else 'cross available'),
            freshness_basis='minimum of book exchange time, metadata and conservative HTTP Date/Age; no stale fallback',
            fees='explicit user assumptions; tier not read; no builder attribution or approvals',
            public_status='public connected / private disabled',
            market_execution='LIMIT GTC/ALO; MARKET means protected aggressive LIMIT IOC, partial or no fill possible')
