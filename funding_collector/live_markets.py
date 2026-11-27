"""Credential-free directory of reviewed live-UI pairs. No inferred asset IDs.

Catalog freshness is not executable depth acceptance: check/start still perform
supervised.validate_public with fresh snapshots before opening either signer.
"""
from pathlib import Path
import math
import time
from .domain import Reject

HL='hyperliquid-mainnet-native'
ENT='entropy-mainnet-io'
LIGHTER={'lighter-core-mainnet':'core','lighter-rh-mainnet':'rh'}
MAX_AGE=60


def public_adapter(root, deployment):
    if deployment==HL:
        from .hyperliquid import HyperliquidPublicAdapter
        return HyperliquidPublicAdapter(root)
    if deployment==ENT:
        from .entropy import EntropyPublicAdapter
        return EntropyPublicAdapter(root)
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
        if not ((primary in (HL,ENT) and hedge in LIGHTER) or
                (hedge in (HL,ENT) and primary in LIGHTER)):
            raise Reject('select independent HL native or Entropy and Lighter Core/RH')

    def _adapter(self, deployment):
        return self.factory(self.root/deployment,deployment)

    @staticmethod
    def _validate(primary, hedge, markets):
        from .adapters import Connection, Deployment
        from .hyperliquid import validate_pair as native_pair
        from .entropy import validate_pair as entropy_pair
        entries=[]
        for dep,market in zip((primary,hedge),markets):
            venue='hyperliquid' if dep==HL else 'entropy' if dep==ENT else 'lighter'
            name='hyperliquid-mainnet-io' if dep==ENT else LIGHTER.get(dep,dep)
            entries.append((Connection(dep,Deployment(name,venue),'public-ui','public-ui'),market))
        (entropy_pair if ENT in (primary,hedge) else native_pair)(entries)

    @staticmethod
    def _leg(market):
        return dict(symbol=market.symbol,asset_id=market.market_id,size_step=str(market.size_step),
                    price_step=str(market.price_step),min_size=str(market.min_size),min_notional=str(market.min_quote))

    def catalog(self, primary, hedge):
        self.deployments(primary,hedge)
        try:
            from .hyperliquid import HyperliquidInstrument
            from .entropy import EntropyInstrument
            from .public import resolve_market
            native=primary if primary in (HL,ENT) else hedge
            light=hedge if hedge in LIGHTER else primary
            catalog=self._adapter(native).catalog()
            raw,at=self._adapter(light).client.get('orderBookDetails')
            observed=min(fresh(catalog['observed_at_unix_s']),fresh(at))
            pairs=[]
            for row in catalog['markets']:
                if not row.get('units_verified') or row.get('is_delisted'):continue
                try:
                    m=(HyperliquidInstrument if native==HL else EntropyInstrument)(row)
                    other=resolve_market(raw,m.base_asset)
                    markets=[m,other] if primary==native else [other,m]
                    self._validate(primary,hedge,markets)
                    pairs.append(dict(base=m.base_asset,label=m.base_asset+' / USD · perpetual',
                                      legs=[self._leg(x) for x in markets]))
                except (Reject,KeyError,TypeError,ValueError):continue
            # A base must resolve unambiguously, never pick the first duplicate.
            pairs=[p for p in pairs if sum(x['base']==p['base'] for x in pairs)==1]
            native_rows=[];light_rows=[]
            for row in catalog['markets']:
                symbol=row.get('coin','')
                supported=bool(row.get('units_verified') and not row.get('is_delisted'))
                native_rows.append(dict(symbol=symbol,label=symbol,base=symbol.removeprefix('io:'),
                    units='base · multiplier 1' if supported else 'непроверенные единицы',supported=supported,
                    reason='' if supported else 'Снят биржей с торгов' if row.get('is_delisted') else 'Не поддерживается приложением: инструмент не проверен'))
            for row in raw['order_book_details']:
                symbol=row.get('symbol','');supported=False
                try:
                    from .public import resolve_market
                    market=resolve_market(raw,symbol)
                    supported=symbol in ('BTC','ETH','SOL','NVDA') and market.multiplier==1
                except (Reject,KeyError,TypeError,ValueError):pass
                light_rows.append(dict(symbol=symbol,label=symbol,base=row.get('base_asset',symbol),
                    units='base · multiplier 1' if supported else 'непроверенные единицы',supported=supported,
                    reason='' if supported else 'Live engine: только проверенные BTC/ETH/SOL/NVDA, active linear perp'))
            listings=[native_rows,light_rows] if primary==native else [light_rows,native_rows]
            return dict(primary=primary,hedge=hedge,pairs=sorted(pairs,key=lambda p:p['base']),markets=listings,
                        observed_at=observed,max_age_seconds=MAX_AGE,
                        warning='Равные base-единицы не гарантируют одинаковый oracle, funding и settlement basis.')
        except Exception:
            raise Reject('public metadata unavailable or unverified; no fallback') from None

    def quote(self, primary, hedge, base=None, *, primary_symbol=None, hedge_symbol=None, side='buy', offset_pct='0', protection_bps='50'):
        from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
        from .domain import number
        offset=number(offset_pct);protection=number(protection_bps)
        if side not in ('buy','sell') or not 0<=offset<100 or not 0<protection<=1000:
            raise Reject('invalid offset or protection')
        catalog=self.catalog(primary,hedge)
        if primary_symbol is None and hedge_symbol is None:
            if not isinstance(base,str) or not 1<=len(base)<=30:raise Reject('invalid base')
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
                if 'asset_id' in leg and (market.symbol,market.market_id)!=(leg['symbol'],leg['asset_id']):raise Reject('metadata changed')
                leg=self._leg(market)
                markets.append(market);times.append(fresh(book.observed_at))
                bid=book.sides['bids'][0][0];ask=book.sides['asks'][0][0]
                reference=(bid+ask)/2
                n=len(legs)
                buy=(side=='buy') if n==0 else (side!='buy')
                fraction=offset/100 if n==0 else protection/10000
                # Primary rounds away from crossing; IOC rounds outward to its explicit bound.
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
