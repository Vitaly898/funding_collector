"""Official SDK 0.24.0 native private bridge. No owner key or generic signing API.

Injected SDK objects must already use the exact owner account and dedicated agent.
No HTTP registration, approvals, builder attribution, leverage, transfers or exit.
"""
import time
from .domain import Reject, number
from .hyperliquid import HyperliquidInstrument
from .live import DEPLOYMENTS, OrderLedger


class HyperliquidPrivate:
    deployment='hyperliquid-mainnet-native'
    dex=''
    instrument_type=HyperliquidInstrument

    def __init__(self, exchange, info, identity, market, authority, journal):
        authority.require_read(identity)
        expected=DEPLOYMENTS[self.deployment][0]
        if (identity.deployment!=self.deployment or not isinstance(market,self.instrument_type)
                or exchange.base_url != expected or info.base_url != expected
                or exchange.vault_address is not None
                or exchange.account_address != identity.account
                or exchange.wallet.address.lower()!=identity.signer.lower()
                or exchange.info.name_to_asset(market.symbol)!=market.market_id
                or info.name_to_asset(market.symbol)!=market.market_id):
            raise Reject('HL SDK account/agent/native deployment/metadata mismatch')
        self.exchange,self.info,self.identity,self.market,self.authority=exchange,info,identity,market,authority
        self.ledger=OrderLedger(journal,identity)

    async def positions(self):
        self.authority.require_read(self.identity)
        raw=self.info.user_state(self.identity.account,dex=self.dex)
        if not isinstance(raw,dict) or not isinstance(raw.get('assetPositions'),list):
            raise Reject('invalid positions response, not empty')
        return [dict(symbol=r['position']['coin'],quantity=str(number(r['position']['szi']))) for r in raw['assetPositions']]

    async def open_orders(self):
        self.authority.require_read(self.identity)
        raw=self.info.open_orders(self.identity.account,dex=self.dex)
        if not isinstance(raw,list):raise Reject('invalid working orders response')
        return raw

    async def order_status(self, client_id):
        from hyperliquid.utils.types import Cloid
        self.authority.require_read(self.identity)
        raw=self.info.query_order_by_cloid(self.identity.account,Cloid.from_str(client_id))
        if raw.get('status')=='unknownOid':return {'status':'UNKNOWN','filled_base':None}
        if raw.get('status')!='order':raise Reject('invalid order status')
        row=raw['order'];o=row['order']
        if o.get('coin')!=self.market.symbol or o.get('cloid')!=client_id:
            raise Reject('order market/client identity mismatch')
        return {'status':row['status'],'order_id':o['oid'],'filled_base':None}

    async def fills(self, order_id):
        self.authority.require_read(self.identity)
        rows=self.info.user_fills(self.identity.account)
        if not isinstance(rows,list) or len(rows)>=2000:
            raise Reject('fill history incomplete; time-window pagination required')
        out=[]
        for r in rows:
            if r['oid']==order_id:
                if r['coin']!=self.market.symbol or r.get('feeToken')!='USDC':
                    raise Reject('fill market/fee currency mismatch')
                out.append(dict(id=str(r['tid']),quantity=str(number(r['sz'],True)),
                                price=str(number(r['px'],True)),fee=str(number(r['fee'])),fee_currency='USDC'))
        if len({r['id'] for r in out})!=len(out):raise Reject('duplicate fill ids')
        return out

    async def submit(self, order):
        from hyperliquid.utils.signing import order_request_to_order_wire,order_wires_to_order_action,sign_l1_action
        from hyperliquid.utils.types import Cloid
        self.authority.check(self.identity,self.market.symbol,self.market.market_id,order)
        self.market.validate(order.quantity,order.price)
        request=dict(coin=self.market.symbol,is_buy=order.side=='buy',sz=float(order.quantity),
                     limit_px=float(order.price),order_type={'limit':{'tif':'Alo' if order.kind=='LIMIT' else 'Ioc'}},
                     reduce_only=False,cloid=Cloid.from_str(order.client_id))
        wire=order_request_to_order_wire(request,self.market.market_id)
        if number(wire['s'])!=order.quantity or number(wire['p'])!=order.price:
            raise Reject('SDK float conversion changed exact asset units')
        action=order_wires_to_order_action([wire],None,'na')
        nonce=self.ledger.reserve(order,int(time.time()*1000),monotonic=True)
        # Official signing, explicit expiry. Skip SDK _post_action debug(payload).
        expiry=int(time.time()*1000)+30000
        signature=sign_l1_action(self.exchange.wallet,action,None,nonce,expiry,True)
        try:
            raw=self.exchange.post('/exchange',dict(action=action,nonce=nonce,signature=signature,
                                                    vaultAddress=None,expiresAfter=expiry))
            statuses=raw['response']['data']['statuses'] if raw.get('status')=='ok' else []
            if len(statuses)!=1:raise ValueError('ambiguous ack')
            s=statuses[0]
            if 'error' in s:
                self.ledger.status(order.client_id,'REJECTED')
                return dict(status='REJECTED',filled_base=None)
            oid=s.get('resting',s.get('filled',{})).get('oid')
            if type(oid) is not int:raise ValueError('missing oid')
            self.ledger.status(order.client_id,'ACK_ONLY')
            return dict(status='ACK_ONLY',order_id=oid,filled_base=None)
        except Exception:
            self.ledger.status(order.client_id,'UNKNOWN')
            return dict(status='UNKNOWN',filled_base=None)

    def close(self):
        self.ledger.close()
