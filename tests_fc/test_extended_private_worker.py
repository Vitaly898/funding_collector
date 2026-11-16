"""Offline synthetic HTTP contracts; real SDK signing uses dummy key 1 only."""
import asyncio
from dataclasses import FrozenInstanceError
from decimal import Decimal as D
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import funding_collector.extended_private as ep
from funding_collector.domain import Reject
from funding_collector.live import Order, Authority
from tests_fc.test_extended import MARKET

SDK = importlib.util.find_spec('x10') is not None

def sdk_market():
    import copy
    raw = copy.deepcopy(MARKET)
    raw.update(assetPrecision=3, collateralAssetPrecision=6,
               l2Config=dict(type='STARKX', syntheticId='0x2', syntheticResolution=1000,
                             collateralId='0x1', collateralResolution=1000000),
               marketStats={k: '0' for k in ('dailyVolume','dailyVolumeBase','dailyPriceChange','dailyLow',
                   'dailyHigh','lastPrice','askPrice','bidPrice','markPrice','indexPrice','fundingRate',
                   'openInterest','openInterestBase')})
    raw['marketStats']['nextFundingRate'] = 1
    raw['tradingConfig'].update(maxPositionValue='1000000',maxLeverage='20',maxNumOrders=100,
                               limitPriceCap='.1',limitPriceFloor='.1',riskFactorConfig=[])
    return raw


def order_row(**changes):
    return dict(dict(id=42, accountId=23, externalId='0x'+'a'*32, market='ETH-USD',type='LIMIT',
                     side='BUY',status='PARTIALLY_FILLED',price='1800',qty='.02',filledQty='.01',
                     reduceOnly=False,postOnly=True,createdTime=1,updatedTime=2,timeInForce='GTT'), **changes)


def trade_row(**changes):
    return dict(dict(id=9,accountId=23,market='ETH-USD',orderId=42,side='BUY',price='1800',
                     qty='.01',value='18',fee='.009',isTaker=True,tradeType='TRADE',createdTime=2), **changes)


def account_row(**changes):
    from fast_stark_crypto import get_public_key
    return dict(dict(id=23, description='synthetic', accountIndex=0, status='ACTIVE',
                     l2Key=hex(get_public_key(1)), l2Vault=7), **changes)


class Response:
    def __init__(self, raw, status=200):
        import json
        self.status = status
        self.body = json.dumps(raw).encode()
        self.content = self
    async def read(self, n):
        result, self.body = self.body[:n], self.body[n:]
        return result
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass


class HTTP:
    def __init__(self): self.calls = []; self.routes = {}
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        route = url.split('/api/v1/')[1]
        value = self.routes.get((method, route), {'status':'OK','data':account_row()} if route == 'user/account/info' else None)
        if isinstance(value, Exception): raise value
        if value is None: raise AssertionError('unexpected offline route ' + route)
        return Response(value)


@unittest.skipUnless(SDK, 'requires isolated pinned Extended SDK')
class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.http = HTTP()
        blocked = patch('socket.socket.connect', side_effect=AssertionError('offline tests forbid network'))
        blocked.start(); self.addCleanup(blocked.stop)
        self.identity = NS(deployment='extended-mainnet-starknet',account='23',signer=account_row()['l2Key'],
                           nonce_scope=('extended-mainnet-starknet','23',account_row()['l2Key'],7))
        self.authority = Authority(self.identity, 'ETH', 2, D('1'), D('5000'), D('50'))
        self.authority.arm('ARM EXACT SCOPED ORDERS', lifetime=60)

    def make(self):
        self.assertTrue(hasattr(ep, 'ExtendedPrivate'), 'worker adapter missing')
        bundle = ep.ExtendedCredentials('dummy-api-only','0x1',self.identity.signer,7)
        return ep.ExtendedPrivate(bundle, self.identity, sdk_market(), self.authority,
                                  Path(self.root.name)/'journal', session=self.http, fee_rate=D('.0005'))

    async def test_constructor_is_offline_and_registration_checks_exact_account(self):
        adapter = self.make()
        self.addCleanup(adapter.close)
        self.assertEqual(self.http.calls, [])
        self.assertEqual((adapter.market.symbol, adapter.market.market_id), ('ETH', 2))
        with self.assertRaisesRegex(Reject,'register'): await adapter.positions()
        self.assertEqual(self.http.calls, [])
        await adapter.register()
        self.http.routes['GET','user/positions'] = {'status':'OK', 'data':[]}
        self.assertEqual(await adapter.positions(), [])
        self.http.routes['GET','user/account/info'] = {'status':'OK','data':account_row(l2Vault=8)}
        with self.assertRaisesRegex(Reject, 'identity'): await adapter.positions()
        self.assertTrue(all(c[2]['allow_redirects'] is False for c in self.http.calls))
    async def test_order_status_partial_is_not_ack_and_checks_identity(self):
        adapter = self.make(); self.addCleanup(adapter.close)
        await adapter.register()
        route = 'user/orders/external/0x'+'a'*32
        self.http.routes['GET',route] = {'status':'OK','data':[order_row()]}
        status = await adapter.order_status('0x'+'a'*32)
        self.assertEqual((status['status'], status['cumulative_base'], status['filled_base']),
                         ('PARTIALLY_FILLED','0.01', None))
        self.http.routes['GET',route] = {'status':'OK','data':[order_row(accountId=24)]}
        with self.assertRaises(Reject): await adapter.order_status('0x'+'a'*32)
        self.http.routes['GET',route] = {'status':'OK','data':[order_row(status='FILLED')]}
        with self.assertRaises(Reject): await adapter.order_status('0x'+'a'*32)
        self.http.routes['GET',route] = {'status':'OK','data':[]}
        self.assertEqual((await adapter.order_status('0x'+'a'*32))['status'],'UNKNOWN')
    async def test_individual_fills_fee_scope_and_pagination_fail_closed(self):
        adapter = self.make(); self.addCleanup(adapter.close)
        await adapter.register()
        self.http.routes['GET','user/trades'] = {'status':'OK','data':[trade_row()]}
        fills = await adapter.fills(42)
        self.assertEqual(fills, [dict(id='9',quantity='0.01',price='1800',fee='0.009',fee_currency='USD')])
        for row in (trade_row(accountId=24),trade_row(market='BTC-USD'),trade_row(fee='NaN'),
                    trade_row(feeCurrency='BTC'),trade_row(value='19')):
            with self.subTest(row=row):
                self.http.routes['GET','user/trades'] = {'status':'OK','data':[row]}
                with self.assertRaises(Reject): await adapter.fills(42)
        self.http.routes['GET','user/trades'] = {'status':'OK','data':[trade_row(),trade_row()]}
        with self.assertRaisesRegex(Reject,'duplicate'): await adapter.fills(42)
        self.http.routes['GET','user/trades'] = {'status':'OK','data':[trade_row()],
                                                'pagination':{'cursor':1,'count':1}}
        with self.assertRaises(Reject): await adapter.fills(42)

    async def test_open_orders_returns_scoped_records_not_empty_on_malformed(self):
        adapter = self.make(); self.addCleanup(adapter.close)
        await adapter.register()
        self.http.routes['GET','user/orders'] = {'status':'OK','data':[order_row()]}
        self.assertEqual((await adapter.open_orders())[0]['order_id'],42)
        self.http.routes['GET','user/orders'] = {'status':'OK','data':None}
        with self.assertRaises(Reject): await adapter.open_orders()
        self.http.routes['GET','user/orders'] = {'status':'OK','data':[order_row(market='BTC-USD')]}
        with self.assertRaises(Reject): await adapter.open_orders()
    def prepare_submit(self):
        self.http.routes['GET','info/markets'] = {'status':'OK','data':[sdk_market()]}
        self.http.routes['GET','user/fees'] = {'status':'OK','data':[dict(market='ETH-USD',
            makerFeeRate='0',takerFeeRate='.0005',builderFeeRate='0')]}
        self.http.routes['POST','user/order'] = {'status':'OK','data':{'id':42,'externalId':'0x'+'a'*32}}
        return Order('0x'+'a'*32,'buy',D('.02'),D('1800'),D('1800'),'LIMIT')

    async def test_real_sdk_dummy_signature_limit_and_protected_market_exact_base(self):
        adapter = self.make(); self.addCleanup(adapter.close)
        order = self.prepare_submit()
        await adapter.register()
        with patch('socket.socket.connect',side_effect=AssertionError('network forbidden')):
            ack = await adapter.submit(order)
        self.assertEqual(ack,dict(status='ACK_ONLY',order_id=42,filled_base=None))
        sent = [call[2]['json'] for call in self.http.calls if call[0]=='POST'][0]
        self.assertEqual((sent['type'],sent['timeInForce'],sent['postOnly'],sent['qty']),
                         ('LIMIT','GTT',True,'0.02'))
        self.assertTrue(int(sent['settlement']['signature']['r'],16)>0)
        self.assertEqual(D(sent['debuggingAmounts']['syntheticAmount']),D('20'))
        with self.assertRaises(Reject): await adapter.submit(order)
        market_order = Order('0x'+'b'*32,'sell',D('.02'),D('1800'),D('1800'),'MARKET')
        self.http.routes['POST','user/order'] = {'status':'OK','data':{'id':43,'externalId':market_order.client_id}}
        await adapter.submit(market_order)
        sent = [call[2]['json'] for call in self.http.calls if call[0]=='POST'][-1]
        self.assertEqual((sent['type'],sent['timeInForce'],sent['postOnly'],sent['qty'],sent['price']),
                         ('MARKET','IOC',False,'0.02','1800'))
        self.assertEqual(D(sent['debuggingAmounts']['syntheticAmount']),D('-20'))

    async def test_timeout_durable_unknown_blocks_new_ids_until_matching_lookup(self):
        adapter = self.make()
        order = self.prepare_submit()
        await adapter.register()
        self.http.routes['POST','user/order'] = TimeoutError('dummy-api-only must not escape')
        self.assertEqual((await adapter.submit(order))['status'],'UNKNOWN')
        adapter.close()
        adapter = self.make(); self.addCleanup(adapter.close)
        await adapter.register()
        with self.assertRaises(Reject): await adapter.submit(order)
        next_order = Order('0x'+'b'*32,'sell',D('.02'),D('1800'),D('1800'),'MARKET')
        with self.assertRaises(Reject): await adapter.submit(next_order)
        route = 'user/orders/external/' + order.client_id
        self.http.routes['GET',route] = {'status':'OK','data':[]}
        self.assertEqual((await adapter.order_status(order.client_id))['status'],'UNKNOWN')
        with self.assertRaises(Reject): await adapter.submit(next_order)
        self.http.routes['GET',route] = {'status':'OK','data':[order_row(qty='.03')]}
        with self.assertRaises(Reject): await adapter.order_status(order.client_id)
        self.http.routes['GET',route] = {'status':'OK','data':[order_row()]}
        await adapter.order_status(order.client_id)
        self.http.routes['POST','user/order'] = {'status':'OK','data':{'id':43,'externalId':next_order.client_id}}
        self.assertEqual((await adapter.submit(next_order))['status'],'ACK_ONLY')
        self.assertEqual(len([c for c in self.http.calls if c[0]=='POST']),2)
    async def test_recovery_cannot_clear_unknown_with_wrong_execution_flags(self):
        adapter = self.make(); self.addCleanup(adapter.close)
        order = self.prepare_submit(); await adapter.register()
        self.http.routes['POST','user/order'] = TimeoutError()
        await adapter.submit(order)
        route = 'user/orders/external/' + order.client_id
        self.http.routes['GET',route] = {'status':'OK','data':[order_row(postOnly=False)]}
        with self.assertRaises(Reject): await adapter.order_status(order.client_id)
        self.assertEqual(adapter.ledger.db.execute('SELECT status FROM orders').fetchone()[0],'UNKNOWN')

    async def test_positions_do_not_hide_other_inventory_or_accept_truncation(self):
        adapter = self.make(); self.addCleanup(adapter.close)
        await adapter.register()
        self.http.routes['GET','user/positions'] = {'status':'OK','data':[],
                                                    'pagination':{'cursor':12,'count':0}}
        with self.assertRaises(Reject): await adapter.positions()
        last = self.http.calls[-1]
        self.assertFalse(last[2]['params'])

    async def test_metadata_copy_cannot_be_mutated_by_caller(self):
        adapter = self.make(); self.addCleanup(adapter.close)
        raw = adapter.instrument.raw
        order = self.prepare_submit(); await adapter.register()
        raw['l2Config']['syntheticResolution'] = 10000
        # Public mutable views cannot change the constructor's trusted binding.
        self.http.routes['GET','info/markets']['data'][0]['l2Config']['syntheticResolution'] = 10000
        with self.assertRaises(Reject): await adapter.submit(order)
    async def test_wal_precedes_sdk_signing_nonce_is_bounded_and_signature_verifies(self):
        import fast_stark_crypto
        from x10.signing.order_object import create_order_object
        from x10.signing import order_object_settlement
        adapter = self.make(); self.addCleanup(adapter.close)
        order = self.prepare_submit(); await adapter.register()
        hashes = []
        real_hash = order_object_settlement.get_order_msg_hash
        def capture_hash(**kwargs):
            result = real_hash(**kwargs); hashes.append(result); return result
        def build(**kwargs):
            row = adapter.ledger.db.execute('SELECT nonce,status FROM orders WHERE id=?',(order.client_id,)).fetchone()
            self.assertEqual(row,(kwargs['nonce'],'PENDING'))
            self.assertLess(kwargs['nonce'],2**32)
            return create_order_object(**kwargs)
        with patch('x10.signing.order_object.create_order_object',side_effect=build), \
             patch('x10.signing.order_object_settlement.get_order_msg_hash',side_effect=capture_hash):
            self.assertEqual((await adapter.submit(order))['status'],'ACK_ONLY')
        sent = [c[2]['json'] for c in self.http.calls if c[0]=='POST'][0]
        sig = sent['settlement']['signature']
        self.assertTrue(fast_stark_crypto.verify(int(self.identity.signer,16),hashes[0],
                                               int(sig['r'],16),int(sig['s'],16)))

    async def test_invalid_precision_market_and_fee_fail_before_signing(self):
        adapter = self.make(); self.addCleanup(adapter.close)
        order = self.prepare_submit(); await adapter.register()
        cases = [('status','REDUCE_ONLY'),('isRfq',True),('active',False)]
        for key, value in cases:
            bad = sdk_market(); bad[key] = value
            self.http.routes['GET','info/markets'] = {'status':'OK','data':[bad]}
            with self.assertRaises(Reject): await adapter.submit(order)
        self.prepare_submit()
        self.http.routes['GET','user/fees']['data'][0]['takerFeeRate'] = '.001'
        with self.assertRaises(Reject): await adapter.submit(order)
        self.assertEqual(adapter.ledger.db.execute('SELECT COUNT(*) FROM orders').fetchone()[0],0)
        self.assertFalse([c for c in self.http.calls if c[0]=='POST'])
        with self.assertRaises(Reject):
            await adapter.submit(Order('0x'+'c'*32,'buy',D('.0201'),D('1800'),D('1800'),'LIMIT'))

    async def test_malformed_ack_stays_unknown_and_payload_is_not_logged(self):
        adapter = self.make(); self.addCleanup(adapter.close)
        order = self.prepare_submit(); await adapter.register()
        self.http.routes['POST','user/order'] = {'status':'OK','data':{'id':42,'externalId':'wrong'}}
        with patch('logging.Logger._log') as log:
            ack = await adapter.submit(order)
        self.assertEqual(ack,dict(status='UNKNOWN',filled_base=None))
        self.assertFalse(log.called)
        self.assertEqual(adapter.ledger.db.execute('SELECT status FROM orders').fetchone()[0],'UNKNOWN')

    async def test_positions_signed_base_and_wrong_account_fail_closed(self):
        adapter = self.make(); self.addCleanup(adapter.close)
        await adapter.register()
        row = dict(id=1,accountId=23,market='ETH-USD',status='OPENED',side='SHORT',leverage='1',
                   size='.02',value='36',openPrice='1800',markPrice='1800',unrealisedPnl='0',
                   realisedPnl='0',createdAt=1,updatedAt=1)
        self.http.routes['GET','user/positions'] = {'status':'OK','data':[row]}
        self.assertEqual(await adapter.positions(),[dict(symbol='ETH',quantity='-0.02')])
        row['accountId'] = 24
        with self.assertRaises(Reject): await adapter.positions()
    async def test_paginated_individual_fills_aggregate_only_matching_order(self):
        adapter = self.make(); self.addCleanup(adapter.close)
        await adapter.register()
        original = self.http.request
        def paged(method, url, **kwargs):
            if url.endswith('/user/trades'):
                if kwargs['params'].get('cursor') == 10:
                    return Response({'status':'OK','data':[trade_row(id=11,fee='-.001')],
                                     'pagination':{'cursor':None,'count':1}})
                return Response({'status':'OK','data':[trade_row(),trade_row(id=10,orderId=99)],
                                 'pagination':{'cursor':10,'count':2}})
            return original(method,url,**kwargs)
        with patch.object(self.http,'request',side_effect=paged):
            rows = await adapter.fills(42)
        self.assertEqual([r['id'] for r in rows],['9','11'])
        self.assertEqual(sum(D(r['quantity']) for r in rows),D('.02'))
        self.assertEqual(sum(D(r['fee']) for r in rows),D('.008'))

    async def test_post_cancellation_is_durable_unknown_not_retryable(self):
        adapter = self.make(); self.addCleanup(adapter.close)
        order = self.prepare_submit(); await adapter.register()
        original = self.http.request
        def cancelled(method,url,**kwargs):
            if method == 'POST': raise asyncio.CancelledError()
            return original(method,url,**kwargs)
        with patch.object(self.http,'request',side_effect=cancelled):
            with self.assertRaises(asyncio.CancelledError): await adapter.submit(order)
        self.assertEqual(adapter.ledger.db.execute('SELECT status FROM orders').fetchone()[0],'UNKNOWN')
        with self.assertRaises(Reject): await adapter.submit(order)

    async def test_transport_deadline_is_real_and_errors_are_sanitized(self):
        adapter = self.make(); self.addCleanup(adapter.close)
        adapter.timeout = 1
        class Stalled:
            async def __aenter__(self): await asyncio.sleep(20)
            async def __aexit__(self,*args): pass
        with patch.object(self.http,'request',return_value=Stalled()):
            with self.assertRaisesRegex(Reject,'no payload retained'):
                await asyncio.wait_for(adapter.register(),timeout=2)
        self.assertFalse(adapter.ready)
        self.http.routes['GET','user/account/info'] = RuntimeError('dummy-api-only')
        with self.assertRaises(Reject) as error: await adapter.register()
        self.assertNotIn('dummy-api-only',str(error.exception))


@unittest.skipUnless(SDK, 'requires isolated pinned Extended SDK')
class CredentialsTests(unittest.TestCase):
    def test_immutable_redacted_bundle_validates_stark_keypair(self):
        self.assertTrue(hasattr(ep, 'ExtendedCredentials'), 'typed credential bundle missing')
        from fast_stark_crypto import get_public_key
        pub = hex(get_public_key(1))
        bundle = ep.ExtendedCredentials('dummy-api-only', '0x1', pub, 7)
        self.assertNotIn('dummy-api-only', repr(bundle))
        self.assertNotIn(pub, repr(bundle))
        with self.assertRaises(FrozenInstanceError): bundle.vault = 8
        with self.assertRaises(Reject): ep.ExtendedCredentials('dummy', '0x1', '0x2', 7)
        with self.assertRaises(Reject): ep.ExtendedCredentials('dummy', '0x1', pub, True)
