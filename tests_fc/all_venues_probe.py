"""Synthetic public metadata and exchange HTTP; real SDKs, keys PUBLIC DUMMIES.

Spawn-safe fixture only; never selectable by the production factory dispatcher.
"""
import time
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import urlsplit,parse_qs
from tests_fc.test_aster import metadata as aster_meta
from tests_fc.test_entropy import inputs,metadata as entropy_meta
from tests_fc.test_hyperliquid import native_meta,native_spot
from tests_fc.test_extended_private_worker import sdk_market,HTTP,Response,order_row,trade_row

ENT='entropy-mainnet-io';ASTER='aster-mainnet';EXT='extended-mainnet-starknet';HL='hyperliquid-mainnet-native'
KEYS={ENT:'01'*32,ASTER:'02'*32,HL:'03'*32}


def extended_raw():
    raw=sdk_market();raw.update(name='XRP-USD',assetName='XRP')
    raw['l2Config']['syntheticId']='0x20000000000001'
    return raw


def public(root,dep):
    if dep.startswith('lighter-'):
        from tests_fc.test_dynamic_native import dynamic_adapters
        return dynamic_adapters(root,dep)
    from funding_collector.hyperliquid import HyperliquidPublicAdapter
    from funding_collector.entropy import EntropyPublicAdapter
    from funding_collector.aster import AsterPublicAdapter
    from funding_collector.extended import ExtendedPublicAdapter
    adapter={HL:HyperliquidPublicAdapter,ENT:EntropyPublicAdapter,ASTER:AsterPublicAdapter,EXT:ExtendedPublicAdapter}[dep](root)
    if dep in (HL,ENT):
        def info(payload):
            kind=payload['type'];now=time.time()
            if kind=='perpDexs':return inputs()[0],now
            if kind=='spotMeta':return (dict(native_spot(),universe=[]) if dep==HL else dict(inputs()[1],universe=[])),now
            if kind=='metaAndAssetCtxs':return (native_meta('XRP') if dep==HL else entropy_meta('io:OAI')),now
            if kind=='l2Book':return dict(coin=payload['coin'],time=int(now*1000),levels=[[dict(px='2999',sz='10',n=1)],[dict(px='3001',sz='10',n=1)]]),now
            raise AssertionError('unreviewed public route')
        adapter.client.info=info
    else:
        def get(route,**params):
            now=time.time()
            if route=='fapi/v3/exchangeInfo':return dict(symbols=[aster_meta('DOGE')]),now
            if route=='fapi/v3/premiumIndex':return dict(symbol='DOGEUSDT',markPrice='3000'),now
            if route=='fapi/v3/depth':return dict(lastUpdateId=1,T=int(now*1000),bids=[['2999','10']],asks=[['3001','10']]),now
            if route=='info/markets':return dict(status='OK',data=[extended_raw()]),now
            if route=='info/markets/XRP-USD/orderbook':return dict(status='OK',data=dict(market='XRP-USD',bid=[dict(price='2999',qty='10')],ask=[dict(price='3001',qty='10')])),now
            raise AssertionError('unreviewed public route')
        adapter.client.get=get
    return adapter


class AsterWire:
    def __init__(self):self.order=None
    async def send(self,method,url,*,body,headers):
        parsed=urlsplit(url);params={k:v[0] for k,v in parse_qs(body if method=='POST' else parsed.query).items()}
        if method=='POST':
            assert self.order is None,'duplicate submit';self.order=params
        route=parsed.path
        if route.endswith('/positionSide/dual'):return dict(dualSidePosition=False)
        if route.endswith('/account'):return dict(canTrade=True)
        if route.endswith('/positionRisk'):return []
        if route.endswith('/openOrders'):return []
        if route.endswith('/order'):
            o=self.order;assert o
            return dict(symbol=o['symbol'],clientOrderId=o['newClientOrderId'],orderId=7,side=o['side'],origQty=o['quantity'],positionSide='BOTH',status='FILLED')
        if route.endswith('/userTrades'):
            o=self.order;assert o
            return [dict(symbol=o['symbol'],orderId=7,id=8,qty=o['quantity'],price=o['price'],commission='.01',commissionAsset='USDT',side=o['side'],positionSide='BOTH')]
        raise AssertionError('unreviewed Aster route')


class ExtendedHTTP(HTTP):
    def __init__(self):super().__init__();self.order=None
    async def close(self):pass
    def request(self,method,url,**kwargs):
        route=url.split('/api/v1/')[1]
        if method=='POST':
            assert self.order is None,'duplicate submit';self.order=kwargs['json']
            assert self.order['market']=='XRP-USD' and self.order['settlement']['signature']
            self.routes[method,route]={'status':'OK','data':dict(id=42,externalId=self.order['id'])}
        if route=='info/markets':self.routes[method,route]={'status':'OK','data':[extended_raw()]}
        if route=='user/fees':self.routes[method,route]={'status':'OK','data':[dict(market='XRP-USD',makerFeeRate='0',takerFeeRate='0.0001',builderFeeRate='0')]}
        if route in ('user/positions','user/orders'):self.routes[method,route]={'status':'OK','data':[]}
        if route.startswith('user/orders/external/'):
            o=self.order;assert o
            self.routes[method,route]={'status':'OK','data':[order_row(market=o['market'],externalId=o['id'],qty=o['qty'],filledQty=o['qty'],price=o['price'],side=o['side'],type=o['type'],timeInForce=o['timeInForce'],postOnly=o['postOnly'],status='FILLED')]}
        if route=='user/trades':
            o=self.order;assert o
            self.routes[method,route]={'status':'OK','data':[trade_row(market=o['market'],qty=o['qty'],price=o['price'],side=o['side'],value=str(Decimal(o['qty'])*Decimal(o['price'])))]}
        return super().request(method,url,**kwargs)


def factory(spec,secret):
    import socket
    from funding_collector.signer_factory import sdk_factory
    patch.object(socket.socket,'connect',side_effect=AssertionError('NO EXCHANGE NETWORK')).start()
    dep=spec.identity.deployment
    bridge,snapshot=sdk_factory(spec,secret,public_factory=lambda *args:public(spec.artifacts,dep),
        aster_transport=AsterWire(),http_session_factory=ExtendedHTTP)
    if dep in (HL,ENT):
        state={'wire':None}
        def info(route,payload):
            wire=state['wire'];kind=payload['type']
            if kind=='extraAgents':return [dict(name='funding-collector:entropy:io' if dep==ENT else 'synthetic-native-agent',address=spec.identity.signer,validUntil=int(time.time()*1000)+60000)]
            if kind=='clearinghouseState':return dict(assetPositions=[] if not wire else [dict(position=dict(coin=spec.symbol,szi=wire['s'] if wire['b'] else '-'+wire['s']))])
            if kind=='openOrders':return []
            if kind=='orderStatus':return dict(status='unknownOid') if not wire else dict(status='order',order=dict(status='filled',order=dict(coin=spec.symbol,cloid=wire['c'],oid=7)))
            if kind=='userFills':return [] if not wire else [dict(oid=7,coin=spec.symbol,feeToken='USDC',tid=8,sz=wire['s'],px=wire['p'],fee='.01')]
            raise AssertionError('unreviewed HL route')
        def exchange(route,payload):
            assert state['wire'] is None,'duplicate submit';assert payload['signature']
            state['wire']=payload['action']['orders'][0]
            assert state['wire']['a']==spec.asset_id
            return dict(status='ok',response=dict(data=dict(statuses=[dict(resting=dict(oid=7))])))
        bridge.info.post=info;bridge.exchange.post=exchange
    return bridge,snapshot
