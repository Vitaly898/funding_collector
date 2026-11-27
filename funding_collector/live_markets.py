"""Independent public instrument directories; no inferred economic equivalence.

Catalogs read metadata only. Quotes fetch current depth/mark for selected rows;
check/start revalidate fresh scopes before constructing either private signer.
"""
from pathlib import Path
import math
import time
from .domain import Reject

HL='hyperliquid-mainnet-native'
ENT='entropy-mainnet-io'
ASTER='aster-mainnet'
EXT='extended-mainnet-starknet'
LIGHTER={'lighter-core-mainnet':'core','lighter-rh-mainnet':'rh'}
SUPPORTED=(HL,ENT,ASTER,EXT,*LIGHTER)
MAX_AGE=60


def public_adapter(root, deployment):
    if deployment==HL:
        from .hyperliquid import HyperliquidPublicAdapter
        return HyperliquidPublicAdapter(root)
    if deployment==ENT:
        from .entropy import EntropyPublicAdapter
        return EntropyPublicAdapter(root)
    if deployment==ASTER:
        from .aster import AsterPublicAdapter
        return AsterPublicAdapter(root)
    if deployment==EXT:
        from .extended import ExtendedPublicAdapter
        return ExtendedPublicAdapter(root)
    if deployment in LIGHTER:
        from .lighter import LighterPublicAdapter
        return LighterPublicAdapter(LIGHTER[deployment],root)
    raise Reject('unsupported directory deployment')


def fresh(at):
    if type(at) not in (int,float) or not math.isfinite(at) or not 0<=time.time()-at<=MAX_AGE:
        raise Reject('stale public metadata')
    return at


class MarketDirectory:
    def __init__(self, root, *, factory=None):
        self.root=Path(root)
        self.factory=factory or public_adapter

    @staticmethod
    def deployments(primary, hedge):
        if primary not in SUPPORTED or hedge not in SUPPORTED or primary==hedge:
            raise Reject('two distinct supported deployments required')

    def _adapter(self, deployment):
        return self.factory(self.root/deployment,deployment)

    @staticmethod
    def _validate(primary, hedge, markets):
        MarketDirectory.deployments(primary,hedge)
        for market in markets:
            if (market.quote_asset!='USD' or market.contract_type!='linear_perpetual'
                    or market.quantity_unit!='base' or market.multiplier!=1 or not market.units_verified):
                raise Reject('unsupported technical contract units')

    @staticmethod
    def _leg(market):
        identity=market.market_id
        # Browser JSON numbers cannot represent wide synthetic IDs / local hashes.
        if type(identity) is not int or identity<0:raise Reject('invalid metadata asset ID')
        return dict(symbol=market.symbol,asset_id=str(identity) if identity>2**53-1 else identity,
                    size_step=str(market.size_step),price_step=str(market.price_step),
                    min_size=str(market.min_size),min_notional=str(market.min_quote))

    @staticmethod
    def _market(deployment,row):
        if deployment==HL:
            from .hyperliquid import HyperliquidInstrument
            return HyperliquidInstrument(row)
        if deployment==ENT:
            from .entropy import EntropyInstrument
            return EntropyInstrument(row)
        if deployment==ASTER:
            from .aster import AsterInstrument
            return AsterInstrument(row)  # no synthetic mark; metadata validation only
        if deployment==EXT:
            from .extended import private_market
            return private_market(row)
        from .public import resolve_market
        return resolve_market(dict(code=200,order_book_details=[row]),row.get('symbol',''))

    @staticmethod
    def _listings(deployment,data):
        if deployment in LIGHTER and data.get('code')!=200:raise Reject('metadata API error')
        source=(data['markets'] if deployment in (HL,ENT) else data['symbols'] if deployment==ASTER
                else data['data'] if deployment==EXT else data['order_book_details'])
        rows=[]
        for row in source:
            symbol=row.get('coin','') if deployment in (HL,ENT) else row.get('name','') if deployment==EXT else row.get('symbol','')
            result=dict(symbol=symbol,label=symbol,base=symbol.removeprefix('io:'),
                        units='непроверенные единицы',supported=False,
                        reason='Снят биржей с торгов' if row.get('is_delisted') else 'Не поддерживается приложением: инструмент не проверен')
            try:
                market=MarketDirectory._market(deployment,row)
                if not market.units_verified or market.multiplier!=1:raise Reject('unsupported units')
                result.update(MarketDirectory._leg(market),symbol=symbol,base=market.base_asset,
                              units='контрактные base-единицы · multiplier 1',supported=True,reason='')
                if deployment==ASTER:result['identity_kind']='local symbol digest; NOT exchange asset index'
            except (Reject,KeyError,TypeError,ValueError):pass
            rows.append(result)
        # Duplicates are never resolved by first-match selection.
        for row in rows:
            if sum(other['symbol']==row['symbol'] for other in rows)!=1:
                row.update(supported=False,reason='Неоднозначный идентификатор в metadata биржи')
        return rows

    def venue(self, deployment):
        if deployment not in SUPPORTED:raise Reject('unsupported directory deployment')
        try:
            adapter=self._adapter(deployment)
            if deployment in (HL,ENT):
                data=adapter.catalog();at=data['observed_at_unix_s']
            else:
                route='fapi/v3/exchangeInfo' if deployment==ASTER else 'info/markets' if deployment==EXT else 'orderBookDetails'
                data,at=adapter.client.get(route)
            return dict(deployment=deployment,markets=self._listings(deployment,data),
                        observed_at=fresh(at),max_age_seconds=MAX_AGE)
        except Exception:
            raise Reject('public metadata unavailable or unverified; no fallback') from None

    def catalog(self, primary, hedge):
        self.deployments(primary,hedge)
        catalogs=[self.venue(dep) for dep in (primary,hedge)]
        listings=[c['markets'] for c in catalogs]
        pairs=[]
        for row in listings[0]:
            matches=[r for r in listings[1] if r['base']==row['base'] and r['supported']]
            if row['supported'] and len(matches)==1:
                legs=[{k:r[k] for k in ('symbol','asset_id','size_step','price_step','min_size','min_notional')} for r in (row,matches[0])]
                pairs.append(dict(base=row['base'],label=row['base']+' / USD · perpetual',legs=legs))
        pairs=[p for p in pairs if sum(x['base']==p['base'] for x in pairs)==1]
        return dict(primary=primary,hedge=hedge,pairs=sorted(pairs,key=lambda p:p['base']),markets=listings,
                    observed_at=min(c['observed_at'] for c in catalogs),max_age_seconds=MAX_AGE,
                    warning='Контрактные единицы разных бирж не гарантируют равную spot/share экспозицию; экономическое соответствие выбирает пользователь. Aster: USDT, USD только расчётный ориентир.')

    def quote(self, primary, hedge, base=None, *, primary_symbol=None, hedge_symbol=None, side='buy', offset_pct='0', protection_bps='50'):
        from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
        from .domain import number
        offset=number(offset_pct);protection=number(protection_bps)
        if side not in ('buy','sell') or not 0<=offset<100 or not 0<protection<=1000:
            raise Reject('invalid offset or protection')
        catalog=self.catalog(primary,hedge)
        if primary_symbol is None and hedge_symbol is None:
            if not isinstance(base,str) or not 1<=len(base)<=40:raise Reject('invalid base')
            matches=[p for p in catalog['pairs'] if p['base']==base]
            if len(matches)!=1:raise Reject('unknown or unsupported pair')
            selected=matches[0]['legs']
        else:
            selected=[]
            for symbol,rows in zip((primary_symbol,hedge_symbol),catalog['markets']):
                matches=[r for r in rows if r['symbol']==symbol and r['supported']]
                if len(matches)!=1:raise Reject('unknown or unsupported instrument')
                selected.append(matches[0])
            base=selected[0]['base']
        try:
            legs=[];markets=[];times=[catalog['observed_at']]
            for dep,leg in zip((primary,hedge),selected):
                market,book,_=self._adapter(dep).snapshot(leg['symbol'])
                # Verify wire identity BEFORE converting to private base-symbol scope.
                if market.symbol!=leg['symbol']:raise Reject('metadata symbol changed')
                if dep==ASTER:market.symbol=market.base_asset
                if dep==EXT:
                    from .extended import private_market
                    market=private_market(market.raw)
                if str(market.market_id)!=str(leg['asset_id']):raise Reject('metadata ID changed')
                leg=self._leg(market)
                markets.append(market);times.append(fresh(book.observed_at))
                bid=book.sides['bids'][0][0];ask=book.sides['asks'][0][0]
                reference=(bid+ask)/2
                n=len(legs);buy=(side=='buy') if n==0 else (side!='buy')
                fraction=offset/100 if n==0 else protection/10000
                up=(not buy) if n==0 else buy
                price=reference*(1+fraction if up else 1-fraction)
                step=market.price_step
                if dep in (HL,ENT) and price<100000:
                    step=max(step,Decimal(10)**(price.adjusted()-4))
                price=(price/step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)*step
                if price<=0:raise Reject('nonpositive rounded price')
                legs.append(dict(leg,bid=str(bid),ask=str(ask),reference=str(reference),price=str(price)))
            self._validate(primary,hedge,markets)
            return dict(base=base,legs=legs,observed_at=min(times),max_age_seconds=MAX_AGE)
        except Exception:
            raise Reject('fresh public quote unavailable; no fallback') from None
