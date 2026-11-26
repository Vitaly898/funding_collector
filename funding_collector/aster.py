"""Aster Futures V3 public-only exact unit-base crypto contracts.

USDT is retained in provenance/raw metadata. Shared USD accounting is indicative,
not a promise of stablecoin parity. No private SDK imports or credentials here.
"""
from pathlib import Path
from types import SimpleNamespace
import time
from .domain import Instrument, Book, Reject, number
from .public import PublicClient

BASE_URL='https://fapi.asterdex.com'
DOCS_REVISION='f3d1c073f63c90d54766e0317e10627ce8031a38'
REVIEWED={'BTCUSDT':0,'ETHUSDT':1,'SOLUSDT':2}  # local identity IDs, NOT exchange asset IDs


class AsterInstrument(Instrument):
    def __init__(self, raw, mark_price):
        symbol=raw.get('symbol')
        if (symbol not in REVIEWED or raw.get('pair')!=symbol or raw.get('baseAsset')+'USDT'!=symbol
                or raw.get('quoteAsset')!='USDT' or raw.get('marginAsset')!='USDT'
                or raw.get('contractType')!='PERPETUAL' or raw.get('status')!='TRADING'
                or raw.get('underlyingType')!='COIN' or 'LIMIT' not in raw.get('orderTypes',[])
                or not {'GTX','IOC'}<=set(raw.get('timeInForce',[]))):
            raise Reject('unreviewed Aster exact active USDT crypto perpetual')
        filters=raw['filters']
        self.filters={f['filterType']:f for f in filters}
        if len(self.filters)!=len(filters):raise Reject('duplicate Aster filters')
        self.mark_price=number(mark_price,True)
        lot=self.filters['LOT_SIZE'];price=self.filters['PRICE_FILTER']
        super().__init__(symbol,REVIEWED[symbol],raw['baseAsset'],'USD','linear_perpetual','base','1',True,
            lot['stepSize'],price['tickSize'],lot['minQty'],self.filters['MIN_NOTIONAL']['notional'],raw=raw)
        for value in (lot['maxQty'],price['minPrice'],price['maxPrice']):number(value,True)
        bounds=self.filters['PERCENT_PRICE']
        if not 0<number(bounds['multiplierDown'],True)<=1<=number(bounds['multiplierUp'],True):
            raise Reject('invalid Aster mark-price bounds')

    def validate(self, qty, price):
        super().validate(qty,price)
        qty,price=number(qty,True),number(price,True)
        pf=self.filters['PRICE_FILTER'];lot=self.filters['LOT_SIZE'];percent=self.filters['PERCENT_PRICE']
        if (qty>number(lot['maxQty'],True) or not number(pf['minPrice'],True)<=price<=number(pf['maxPrice'],True)
                or not self.mark_price*number(percent['multiplierDown'])<=price<=self.mark_price*number(percent['multiplierUp'])):
            raise Reject('Aster quantity/price/mark filter violation')


class AsterPublicClient(PublicClient):
    def __init__(self, artifacts):
        self.deployment=SimpleNamespace(name='aster',base_url=BASE_URL,chain_id=1666)
        self.artifacts=Path(artifacts);self.artifacts.mkdir(parents=True,exist_ok=True)

    def success(self, raw):
        return isinstance(raw,dict) and 'code' not in raw

    def valid_route(self, route, params):
        return ((route=='fapi/v3/exchangeInfo' and not params)
            or (route=='fapi/v3/depth' and set(params)=={'symbol','limit'}
                and params['symbol'] in REVIEWED and type(params['limit']) is int and params['limit']==100)
            or (route=='fapi/v3/premiumIndex' and set(params)=={'symbol'} and params['symbol'] in REVIEWED))


class AsterPublicAdapter:
    def __init__(self, artifacts):self.client=AsterPublicClient(artifacts)

    def snapshot(self, symbol):
        if symbol not in REVIEWED:raise Reject('exact reviewed Aster symbol required (BTCUSDT/ETHUSDT/SOLUSDT)')
        raw,_=self.client.get('fapi/v3/exchangeInfo')
        matches=[r for r in raw['symbols'] if r.get('symbol')==symbol]
        if len(matches)!=1:raise Reject('Aster exact market ambiguous/missing')
        mark,mark_at=self.client.get('fapi/v3/premiumIndex',symbol=symbol)
        if mark.get('symbol')!=symbol:raise Reject('Aster mark symbol mismatch')
        market=AsterInstrument(matches[0],mark['markPrice'])
        response,at=self.client.get('fapi/v3/depth',symbol=symbol,limit=100)
        if type(response.get('lastUpdateId')) is not int:raise Reject('invalid Aster depth identity')
        event=response.get('T')
        if type(event) is not int or event<=0:raise Reject('missing Aster transaction timestamp')
        observed=min(at,mark_at,event/1000)
        normalized={name:[dict(price=p,remaining_base_amount=q) for p,q in response[name]] for name in ('bids','asks')}
        return market,Book(normalized,observed),dict(base_url=BASE_URL,docs_revision=DOCS_REVISION,
            price_currency='USDT',collateral_currency='USDT',shared_quote='USD indicative only; no parity guarantee',
            book_observed_at_unix_s=observed,exchange_event_timestamp=event,
            freshness_basis='minimum of HTTP freshness, mark receipt and depth transaction T',
            book_age_seconds_at_preview=time.time()-observed,public_status='public only; account acceptance unverified')
