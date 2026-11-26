"""Bounded Aster Futures V3 private bridge.

Local fences are not agent permissions. No registration, approvals, transfers,
withdrawals, leverage changes or generic public signing entry point exists.
The optional transport is an HTTP-only ``send(method, url, *, body, headers)``
seam: it receives the final signed wire, never a key or a signing callback.
"""
import asyncio
import re
import time
from .aster import AsterInstrument, BASE_URL, REVIEWED
from .domain import Reject, number
from .live import DEPLOYMENTS, OrderLedger


class HTTPTransport:
    """No redirects, ambient proxies, retries or request logging."""
    async def send(self, method, url, *, body, headers):
        return await asyncio.to_thread(self._send,method,url,body,headers)

    @staticmethod
    def _send(method,url,body,headers):
        import json
        import urllib.request
        from urllib.parse import urlsplit
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.netloc!='fapi.asterdex.com' or parsed.username or parsed.fragment:
            raise Reject('Aster fixed HTTPS origin required')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        request=urllib.request.Request(url,data=body.encode() if body is not None else None,method=method,headers=headers)
        try:
            with opener.open(request,timeout=10) as response:
                if response.geturl()!=url:raise ValueError()
                raw=response.read(1024*1024+1)
                if len(raw)>1024*1024:raise ValueError()
                return json.loads(raw)
        except Exception:raise Reject('Aster private HTTP unavailable; no automatic retry') from None


class AsterPrivate:
    def __init__(self, identity, secret, market, authority, journal, *, transport=None):
        from .keystore import _binding
        try:
            authority.require_read(identity)
            if (identity.deployment != 'aster-mainnet'
                    or DEPLOYMENTS[identity.deployment] != (BASE_URL, 1666)
                    or _binding(identity.deployment, identity.account, identity.api_key_index, secret) != identity
                    or not isinstance(market, AsterInstrument)
                    or market.raw.get('symbol') not in REVIEWED
                    or market.symbol != market.base_asset
                    or market.symbol + 'USDT' != market.raw['symbol']
                    or market.market_id != REVIEWED[market.raw['symbol']]
                    or market.multiplier != 1 or market.quantity_unit != 'base'
                    or market.quote_asset != 'USD'
                    or identity.account.lower() == identity.signer.lower()
                    or identity.api_key_index is not None):
                raise ValueError()
            from eth_account import Account
            self._wallet = Account.from_key(secret)
        except Exception:
            raise Reject('Aster deployment/account/agent/market binding mismatch') from None
        self.identity, self.market, self.authority = identity, market, authority
        self.ledger = OrderLedger(journal, identity)
        self._transport = transport
        self._lock = asyncio.Lock()

    async def _request(self, method, route, params, *, nonce=None):
        from urllib.parse import urlencode
        from eth_account.messages import encode_typed_data
        allowed={('GET','/fapi/v3/positionSide/dual'):set(),('GET','/fapi/v3/account'):set(),
                 ('GET','/fapi/v3/positionRisk'):set(),('GET','/fapi/v3/openOrders'):set(),
                 ('GET','/fapi/v3/order'):{'symbol','origClientOrderId'},
                 ('GET','/fapi/v3/userTrades'):{'symbol','orderId','limit'},
                 ('POST','/fapi/v3/order'):{'symbol','side','positionSide','type','timeInForce','quantity','price','newClientOrderId','reduceOnly','newOrderRespType'}}
        self.authority.require_read(self.identity)
        if (method,route) not in allowed or set(params)!=allowed[(method,route)]:raise Reject('Aster route/fields forbidden')
        if 'symbol' in params and params['symbol']!=self.market.raw['symbol']:raise Reject('Aster wire symbol mismatch')
        async with self._lock:
            n=nonce if nonce is not None else max(int(time.time()*1000000),getattr(self,'_last_nonce',0)+1)
            if type(n) is not int or n<=getattr(self,'_last_nonce',0) or abs(n-int(time.time()*1000000))>50000000:raise Reject('Aster nonce outside local freshness/sequence scope')
            self._last_nonce=n
            fields=dict(params,user=self.identity.account,signer=self.identity.signer,nonce=n,nonceWindow=5000)
            payload=urlencode(fields)
            message=encode_typed_data(domain_data=dict(name='AsterSignTransaction',version='1',chainId=1666,verifyingContract='0x'+'00'*20),
                message_types={'Message':[dict(name='msg',type='string')]},message_data={'msg':payload})
            signature=self._wallet.sign_message(message).signature.hex()
            wire=payload+'&signature='+signature
            url=BASE_URL+route+('?' + wire if method=='GET' else '')
            transport=self._transport or HTTPTransport()
            try:
                raw=await transport.send(method,url,body=wire if method=='POST' else None,headers={'Content-Type':'application/x-www-form-urlencoded'})
                if isinstance(raw,dict) and 'code' in raw and raw['code'] not in (0,200):raise ValueError()
                return raw
            except Exception:raise Reject('Aster private request failed; execution outcome may be unknown') from None

    async def registration(self):
        mode=await self._request('GET','/fapi/v3/positionSide/dual',{})
        if not isinstance(mode,dict) or mode.get('dualSidePosition') is not False:raise Reject('Aster requires existing one-way mode; no settings changed')
        account=await self._request('GET','/fapi/v3/account',{})
        if not isinstance(account,dict) or account.get('canTrade') is not True:raise Reject('Aster account trading eligibility unverified')

    async def positions(self):
        raw=await self._request('GET','/fapi/v3/positionRisk',{})
        if not isinstance(raw,list):raise Reject('Aster positions unavailable')
        out=[]
        for r in raw:
            if r.get('positionSide')!='BOTH':raise Reject('Aster unsupported hedge mode')
            symbol=r.get('symbol','')
            if not symbol.endswith('USDT'):raise Reject('Aster position units unknown')
            out.append(dict(symbol=symbol[:-4],quantity=str(number(r['positionAmt']))))
        return out

    async def open_orders(self):
        raw=await self._request('GET','/fapi/v3/openOrders',{})
        if not isinstance(raw,list) or len(raw)>=1000:raise Reject('Aster open orders incomplete')
        return raw

    def _owned(self, client_id):
        import json
        row=self.ledger.db.execute('SELECT request FROM orders WHERE id=?',(client_id,)).fetchone()
        if not row:raise Reject('Aster unknown local order identity')
        return json.loads(row[0])

    def _bind_ack(self, raw, client_id):
        expected=self._owned(client_id)
        if (not isinstance(raw,dict) or raw.get('symbol')!=self.market.raw['symbol'] or raw.get('clientOrderId')!=client_id
            or type(raw.get('orderId')) is not int or raw['orderId']<0 or raw.get('side')!=expected['side'].upper()
            or raw.get('positionSide')!='BOTH' or number(raw.get('origQty'),True)!=number(expected['quantity'])):
            raise Reject('Aster order acknowledgement scope mismatch')
        self.ledger.db.execute('CREATE TABLE IF NOT EXISTS aster_ids (order_id INTEGER PRIMARY KEY, client_id TEXT UNIQUE NOT NULL)')
        existing=self.ledger.db.execute('SELECT client_id FROM aster_ids WHERE order_id=?',(raw['orderId'],)).fetchone()
        if existing and existing[0]!=client_id:raise Reject('Aster order identity collision')
        self.ledger.db.execute('INSERT OR IGNORE INTO aster_ids VALUES (?,?)',(raw['orderId'],client_id));self.ledger.db.commit()
        return raw['orderId']

    async def order_status(self, client_id):
        self._owned(client_id)
        try:
            raw=await self._request('GET','/fapi/v3/order',dict(symbol=self.market.raw['symbol'],origClientOrderId=client_id))
            oid=self._bind_ack(raw,client_id)
            if raw.get('status') not in ('NEW','PARTIALLY_FILLED','FILLED','CANCELED','EXPIRED','REJECTED'):raise Reject('unknown order status')
            self.ledger.status(client_id,'ACK_ONLY')
            return dict(status=raw['status'],order_id=oid,filled_base=None)
        except Reject:return dict(status='UNKNOWN',filled_base=None)

    async def fills(self, order_id):
        import json
        self.ledger.db.execute('CREATE TABLE IF NOT EXISTS aster_ids (order_id INTEGER PRIMARY KEY, client_id TEXT UNIQUE NOT NULL)')
        own=self.ledger.db.execute('SELECT client_id FROM aster_ids WHERE order_id=?',(order_id,)).fetchone()
        if not own:raise Reject('foreign Aster order id refused')
        expected=self._owned(own[0])
        raw=await self._request('GET','/fapi/v3/userTrades',dict(symbol=self.market.raw['symbol'],orderId=order_id,limit=1000))
        if not isinstance(raw,list) or len(raw)>=1000:raise Reject('Aster fill page incomplete; manual reconciliation required')
        out=[];seen=set()
        for r in raw:
            if (r.get('symbol')!=self.market.raw['symbol'] or r.get('orderId')!=order_id or type(r.get('id')) is not int
                or r['id'] in seen or r.get('commissionAsset')!='USDT' or r.get('positionSide')!='BOTH'
                or r.get('side')!=expected['side'].upper()):raise Reject('Aster fill identity/fee mismatch')
            seen.add(r['id'])
            out.append(dict(id=str(r['id']),quantity=str(number(r['qty'],True)),price=str(number(r['price'],True)),fee=str(number(r['commission'])),fee_currency='USDT'))
        if sum((number(r['quantity']) for r in out),number('0'))>number(expected['quantity']):raise Reject('Aster overfill')
        return out

    async def submit(self, order):
        self.authority.check(self.identity,self.market.symbol,self.market.market_id,order)
        self.market.validate(order.quantity,order.price)
        await self.registration()
        self.authority.check(self.identity,self.market.symbol,self.market.market_id,order)
        nonce=self.ledger.reserve(order,max(int(time.time()*1000000),getattr(self,'_last_nonce',0)+1),monotonic=True)
        params=dict(symbol=self.market.raw['symbol'],side=order.side.upper(),positionSide='BOTH',type='LIMIT',
            timeInForce='GTX' if order.kind=='LIMIT' else 'IOC',quantity=str(order.quantity),price=str(order.price),
            newClientOrderId=order.client_id,reduceOnly='false',newOrderRespType='ACK')
        try:
            raw=await self._request('POST','/fapi/v3/order',params,nonce=nonce)
            oid=self._bind_ack(raw,order.client_id)
            self.ledger.status(order.client_id,'ACK_ONLY')
            return dict(status='ACK_ONLY',order_id=oid,filled_base=None)
        except Exception:
            self.ledger.status(order.client_id,'UNKNOWN')
            return dict(status='UNKNOWN',filled_base=None)

    def close(self):
        self.ledger.close()
        self._wallet = None
