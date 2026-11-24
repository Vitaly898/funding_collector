"""Offline Aster V3 contracts. Public dummy key; no private HTTP."""
import asyncio
from decimal import Decimal as D
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from eth_account import Account
from funding_collector.aster import AsterInstrument
from funding_collector.domain import Reject
from funding_collector.live import Identity, Authority, Order
from tests_fc.test_aster import metadata

DUMMY = '01' * 32
OWNER = '0x' + '22' * 20


class Wire:
    def __init__(self):
        self.calls = []
        self.responses = {}

    async def send(self, method, url, *, body, headers):
        from urllib.parse import urlsplit, parse_qs
        parsed = urlsplit(url)
        params = parse_qs(parsed.query if method == 'GET' else body)
        self.calls.append((method, parsed.path, params, parsed.query if method == 'GET' else body))
        result = self.responses.get((method, parsed.path))
        if isinstance(result, Exception):
            raise result
        return result


class AsterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guard = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        self.guard.start()
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.addCleanup(self.guard.stop)
        self.wire = Wire()

    def make(self, **changes):
        from funding_collector.live_aster import AsterPrivate
        identity = Identity('aster-mainnet', OWNER, Account.from_key(DUMMY).address)
        market = AsterInstrument(metadata(), '2000')
        market.symbol = market.base_asset
        authority = Authority(identity, 'ETH', 1, D('1'), D('5000'), D('100'))
        values = dict(identity=identity, secret=DUMMY, market=market, authority=authority,
                      journal=Path(self.root.name)/'journal', transport=self.wire)
        values.update(changes)
        adapter = AsterPrivate(**values)
        self.addCleanup(adapter.close)
        return adapter

    async def test_signed_readonly_and_protected_orders(self):
        from eth_account.messages import encode_typed_data
        from urllib.parse import parse_qsl
        adapter=self.make()
        self.wire.responses.update({('GET','/fapi/v3/positionSide/dual'):{'dualSidePosition':False},
            ('GET','/fapi/v3/account'):{'canTrade':True},('GET','/fapi/v3/openOrders'):[],
            ('GET','/fapi/v3/positionRisk'):[{'symbol':'ETHUSDT','positionSide':'BOTH','positionAmt':'0'}]})
        await adapter.registration()
        self.assertEqual(await adapter.positions(),[{'symbol':'ETH','quantity':'0'}])
        self.assertEqual(await adapter.open_orders(),[])
        order=Order('0x'+'ab'*16,'buy','.01','1999.99','2000','LIMIT')
        with self.assertRaises(Reject):await adapter.submit(order)
        self.assertTrue(all(c[0]=='GET' for c in self.wire.calls))
        adapter.authority.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
        self.wire.responses[('POST','/fapi/v3/order')]={'symbol':'ETHUSDT','clientOrderId':order.client_id,'orderId':7,'side':'BUY','origQty':'.01','positionSide':'BOTH','status':'NEW'}
        result=await adapter.submit(order)
        self.assertEqual(result,{'status':'ACK_ONLY','order_id':7,'filled_base':None})
        _,route,params,wire=self.wire.calls[-1]
        self.assertEqual((route,params['type'],params['timeInForce'],params['quantity'],params['positionSide']),('/fapi/v3/order',['LIMIT'],['GTX'],['0.01'],['BOTH']))
        payload,signature=wire.rsplit('&signature=',1)
        message=encode_typed_data(domain_data={'name':'AsterSignTransaction','version':'1','chainId':1666,'verifyingContract':'0x'+'00'*20},message_types={'Message':[{'name':'msg','type':'string'}]},message_data={'msg':payload})
        self.assertEqual(Account.recover_message(message,signature=signature),adapter.identity.signer)
        self.assertEqual(params['nonceWindow'],['5000'])
        with self.assertRaises(Reject):await adapter.submit(order)
        self.wire.responses[('GET','/fapi/v3/order')]={'symbol':'ETHUSDT','clientOrderId':order.client_id,'orderId':7,'side':'BUY','origQty':'.01','positionSide':'BOTH','status':'FILLED'}
        self.assertEqual((await adapter.order_status(order.client_id))['status'],'FILLED')
        self.wire.responses[('GET','/fapi/v3/userTrades')]=[dict(symbol='ETHUSDT',orderId=7,id=8,qty='.01',price='1999.99',commission='.01',commissionAsset='USDT',side='BUY',positionSide='BOTH')]
        self.assertEqual((await adapter.fills(7))[0],dict(id='8',quantity='0.01',price='1999.99',fee='0.01',fee_currency='USDT'))

    async def test_ioc_unknown_no_retry_and_forbidden_routes(self):
        adapter=self.make();adapter.authority.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
        self.wire.responses[('GET','/fapi/v3/positionSide/dual')]={'dualSidePosition':False}
        self.wire.responses[('GET','/fapi/v3/account')]={'canTrade':True}
        self.wire.responses[('POST','/fapi/v3/order')]=TimeoutError('private transport failure')
        order=Order('0x'+'ac'*16,'sell','.01','1999.99','2000','MARKET')
        self.assertEqual((await adapter.submit(order))['status'],'UNKNOWN')
        self.assertEqual(self.wire.calls[-1][2]['timeInForce'],['IOC'])
        with self.assertRaises(Reject):await adapter.submit(Order('0x'+'ad'*16,'sell','.01','1999.99','2000','MARKET'))
        with self.assertRaises(Reject):await adapter._request('POST','/fapi/v3/leverage',{})
        self.assertEqual(sum(c[0]=='POST' for c in self.wire.calls),1)

    async def test_arm_revoked_during_readonly_preflight_never_posts(self):
        adapter=self.make();adapter.authority.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
        self.wire.responses[('GET','/fapi/v3/positionSide/dual')]={'dualSidePosition':False}
        self.wire.responses[('GET','/fapi/v3/account')]={'canTrade':True}
        original=self.wire.send
        async def revoke(method,url,**kwargs):
            result=await original(method,url,**kwargs)
            if '/fapi/v3/account?' in url:adapter.authority.lock()
            return result
        self.wire.send=revoke
        with self.assertRaises(Reject):
            await adapter.submit(Order('0x'+'ae'*16,'buy','.01','1999.99','2000','LIMIT'))
        self.assertTrue(all(c[0]=='GET' for c in self.wire.calls))
        self.assertEqual(adapter.ledger.db.execute('SELECT COUNT(*) FROM orders').fetchone()[0],0)

    async def test_wrong_mode_and_foreign_fills_refused(self):
        adapter=self.make()
        self.wire.responses[('GET','/fapi/v3/positionSide/dual')]={'dualSidePosition':True}
        with self.assertRaises(Reject):await adapter.registration()
        with self.assertRaises(Reject):await adapter.fills(777)

    async def test_constructor_bound_and_offline(self):
        adapter = self.make()
        self.assertEqual(adapter.market.raw['symbol'], 'ETHUSDT')
        self.assertEqual(self.wire.calls, [])
        self.assertEqual(adapter.authority._until, 0)
