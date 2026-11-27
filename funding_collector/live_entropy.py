"""Entropy's bounded private io bridge via official HL SDK, never native fallback.

Dedicated agent name is an application registration fence, NOT a protocol-level
permission. HL agents are account-wide and nonces are shared across dexes; do not
reuse this key with native HL or any other app. No registration/approval API.
"""
import time
from .domain import Reject
from .entropy import EntropyInstrument, USDC_TOKEN_ID, contract_identity
from .live_hyperliquid import HyperliquidPrivate

AGENT_NAME='funding-collector:entropy:io'


class EntropyPrivate(HyperliquidPrivate):
    deployment='entropy-mainnet-io'
    dex='io'
    instrument_type=EntropyInstrument

    def __init__(self, exchange, info, identity, market, authority, journal):
        raw=market.raw
        if (market.base_asset!=market.symbol.removeprefix('io:')
                or raw.get('dex')!='io' or raw.get('coin')!=market.symbol
                or raw.get('underlying_id')!=contract_identity(market.symbol)
                or raw.get('collateral_asset')!='USDC'
                or raw.get('collateral_token_id')!=USDC_TOKEN_ID
                or not raw.get('units_verified') or raw.get('is_delisted')
                or type(raw.get('dex_index')) is not int or raw['dex_index']<1
                or type(raw.get('index_in_meta')) is not int or not 0<=raw['index_in_meta']<10000
                or market.market_id!=100000+raw['dex_index']*10000+raw['index_in_meta']):
            raise Reject('Entropy exact io contract metadata/collateral scope required')
        super().__init__(exchange,info,identity,market,authority,journal)

    async def registration(self):
        self.authority.require_read(self.identity)
        # Official extraAgents is owner-scoped, not dex-scoped. Never claim otherwise.
        rows=self.info.extra_agents(self.identity.account)
        if not isinstance(rows,list):raise Reject('Entropy agent registration unavailable')
        matches=[r for r in rows if isinstance(r,dict)
                 and str(r.get('address','')).lower()==self.identity.signer.lower()]
        if (len(matches)!=1 or matches[0].get('name')!=AGENT_NAME
                or type(matches[0].get('validUntil')) is not int
                or matches[0]['validUntil']<=int(time.time()*1000)+30000):
            raise Reject('dedicated Entropy namespace agent missing or expiring; no signing')

    async def submit(self, order):
        self.authority.check(self.identity,self.market.symbol,self.market.market_id,order)
        await self.registration()
        return await super().submit(order)

    async def positions(self):
        rows=await super().positions()  # clearinghouseState explicitly dex='io'
        if any(not r['symbol'].startswith('io:') for r in rows):
            raise Reject('Entropy positions wrong dex namespace')
        return rows

    async def open_orders(self):
        rows=await super().open_orders()  # openOrders explicitly dex='io'
        if any(not isinstance(r,dict) or not str(r.get('coin','')).startswith('io:') for r in rows):
            raise Reject('Entropy open orders wrong dex namespace')
        return rows

    # Official orderStatus and userFills have no dex selector. The inherited
    # implementations require the exact scoped symbol for every matched cloid/order ID,
    # require USDC fees, reject incomplete history and never fall back to native.
