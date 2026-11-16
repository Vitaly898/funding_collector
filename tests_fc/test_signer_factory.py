"""Default hydration: genuine SDKs, obvious dummy keys, transport only mocked."""
import asyncio
from dataclasses import replace
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from funding_collector.domain import Book, Reject
from funding_collector.keystore import _binding
from funding_collector.live import Order
from funding_collector.signer_session import SessionSpec, validate_snapshot
from tests_fc.test_hyperliquid import native_meta, native_spot


def spec(root, deployment='hyperliquid-mainnet-native'):
    key='01'*(32 if deployment.startswith('hyperliquid') else 40)
    identity=_binding(deployment,'0x'+'11'*20 if deployment.startswith('hyperliquid') else '123',
                      None if deployment.startswith('hyperliquid') else 4,key)
    return SessionSpec(identity,'ETH',0,'.1','500','50',str(Path(root)/'journal'),str(Path(root)/'public')),key


def book():
    return Book(dict(bids=[dict(price='2999',remaining_base_amount='1')],
                     asks=[dict(price='3001',remaining_base_amount='1')]),time.time())


class FactoryTests(unittest.TestCase):
    def test_default_native_hydrates_real_sdk_offline_and_roundtrips(self):
        from funding_collector.signer_factory import sdk_factory
        from funding_collector.hyperliquid import HyperliquidPublicAdapter, HyperliquidInstrument, resolve_contract
        from hyperliquid.api import API
        from hyperliquid.exchange import Exchange
        with tempfile.TemporaryDirectory() as root:
            scope,key=spec(root)
            market=HyperliquidInstrument(resolve_contract(native_meta(),native_spot(),'ETH'))
            adapter=Mock();adapter._metadata.return_value=(dict(native_spot(),universe=[]),native_meta(),time.time())
            adapter.snapshot.side_effect=lambda symbol:(market,book(),{})
            with patch.object(socket.socket,'connect',side_effect=AssertionError('NO NETWORK')), \
                 patch('funding_collector.hyperliquid.HyperliquidPublicAdapter',return_value=adapter), \
                 patch.object(API,'post',side_effect=AssertionError('NO CONSTRUCTOR HTTP')):
                bridge,snapshot=sdk_factory(scope,key)
                self.assertIsInstance(bridge.exchange,Exchange)
                self.assertEqual(bridge.exchange.info.coin_to_asset,{'ETH':0})
                self.assertEqual(bridge.exchange.account_address,scope.identity.account)
                self.assertIsNone(bridge.exchange.info.ws_manager)
                self.assertEqual(bridge.authority._until,0)
                bridge.authority.arm('ARM EXACT SCOPED ORDERS',lifetime=10)
                order=Order('0x'+'ab'*16,'buy','.02','3000','3000','LIMIT')
                validate_snapshot(bridge,snapshot(),order)
                async def run():
                    with patch.object(bridge.info,'post',side_effect=[
                        {'assetPositions':[{'position':{'coin':'ETH','szi':'.02'}}]},[],
                        [{'address':scope.identity.signer,'validUntil':int(time.time()*1000)+60000}],
                        {'status':'order','order':{'status':'filled','order':{'coin':'ETH','cloid':order.client_id,'oid':7}}},
                        [{'oid':7,'coin':'ETH','feeToken':'USDC','tid':9,'sz':'.02','px':'3000','fee':'.01'}]]) as reads, \
                         patch.object(bridge.exchange,'post',return_value={'status':'ok','response':{'data':{'statuses':[{'resting':{'oid':7}}]}}}) as post:
                        self.assertEqual((await bridge.positions())[0]['quantity'],'0.02')
                        self.assertEqual(await bridge.open_orders(),[])
                        self.assertEqual((await bridge.submit(order))['status'],'ACK_ONLY')
                        self.assertEqual((await bridge.order_status(order.client_id))['order_id'],7)
                        self.assertEqual((await bridge.fills(7))[0]['fee'],'0.01')
                        action=post.call_args.args[1]['action']
                        self.assertNotIn('builder',action)
                        self.assertEqual(action['orders'][0]['a'],scope.asset_id)
                        self.assertTrue(all(c.args[1]['user']==scope.identity.account for c in reads.call_args_list))
                asyncio.run(run());bridge.close()

    def test_default_core_real_sdk_roundtrip_and_no_integrator_fees(self):
        from funding_collector.signer_factory import sdk_factory
        from funding_collector.lighter import LighterPublicAdapter, normalize_market
        from unittest.mock import AsyncMock
        from types import SimpleNamespace as NS
        import json
        import lighter
        with tempfile.TemporaryDirectory() as root:
            scope,key=spec(root,'lighter-core-mainnet')
            market=normalize_market(dict(symbol='ETH',market_id=0,market_type='perp',status='active',
                supported_size_decimals=4,supported_price_decimals=2,min_base_amount='.001',min_quote_amount='10'))
            public=Mock();public.snapshot.side_effect=lambda symbol:(market,book(),{})
            accounts=NS(account=AsyncMock(return_value=NS(code=200,accounts=[NS(account_index=123,
                positions=[NS(symbol='ETH',position='.02',sign=1)])])))
            async def run():
                with patch('funding_collector.lighter.LighterPublicAdapter',return_value=public), \
                     patch.object(lighter,'AccountApi',return_value=accounts), \
                     patch.object(socket.socket,'connect',side_effect=AssertionError('NO NETWORK')):
                    bridge,snapshot=sdk_factory(scope,key)
                    s=bridge.signer
                    self.assertIsInstance(s,lighter.SignerClient)
                    self.assertEqual((s.chain_id,s.account_index,list(s.api_key_dict)),(304,123,[4]))
                    self.assertIsInstance(s.nonce_manager,lighter.nonce_manager.NoOpNonceManager)
                    bridge.authority.arm('ARM EXACT SCOPED ORDERS',lifetime=10)
                    order=Order('0x'+'ab'*16,'buy','.02','3000','3000','LIMIT')
                    validate_snapshot(bridge,snapshot(),order)
                    s.check_client=Mock(return_value=None)
                    s.tx_api.next_nonce=AsyncMock(return_value=NS(code=200,nonce=1))
                    s.tx_api.send_tx=AsyncMock(return_value=NS(code=200))
                    signed=[];original=s.sign_create_order
                    def sign(**kw):
                        result=original(**kw);signed.append((kw,json.loads(result[1])));return result
                    with patch.object(s,'sign_create_order',side_effect=sign):
                        self.assertEqual((await bridge.positions())[0]['quantity'],'0.02')
                        result=await bridge.submit(order)
                    self.assertEqual(result['status'],'ACK_ONLY');s.check_client.assert_called_once_with()
                    kw,tx=signed[0]
                    self.assertEqual(tuple(kw[n] for n in ('integrator_account_index','integrator_taker_fee','integrator_maker_fee')),(0,0,0))
                    self.assertEqual((tx['AccountIndex'],tx['ApiKeyIndex'],tx['MarketIndex']),(123,4,0))
                    self.assertEqual((tx['BaseAmount'],tx['Price']),(200,300000))
                    self.assertEqual(tuple(tx.get(n,0) for n in ('IntegratorAccountIndex','IntegratorTakerFee','IntegratorMakerFee')),(0,0,0))
                    s.order_api.account_active_orders=AsyncMock(return_value=NS(code=200,orders=[]))
                    self.assertEqual(await bridge.open_orders(),[])
                    row=NS(client_order_index=result['client_order_index'],order_index=7,owner_account_index=123,
                           market_index=0,status='filled',filled_base_amount='.02')
                    s.order_api.account_inactive_orders=AsyncMock(return_value=NS(code=200,orders=[row],next_cursor=None))
                    self.assertEqual((await bridge.order_status(order.client_id))['order_id'],7)
                    s.order_api.trades=AsyncMock(return_value=NS(code=200,next_cursor=None,trades=[NS(
                        trade_id=9,market_id=0,ask_account_id=123,bid_account_id=321,ask_id=7,bid_id=8,
                        is_maker_ask=False,size='.02',price='3000',usd_amount='60',taker_fee=200,maker_fee=0,
                        integrator_taker_fee=0,integrator_maker_fee=0)]))
                    self.assertEqual((await bridge.fills(7))[0]['fee'],'0.012')
                    accounts.account.assert_awaited_once_with(by='index',value='123')
                    bridge.close();await s.close()
            asyncio.run(run())

    def test_wrong_secret_refused_before_public_or_sdk_construction(self):
        from funding_collector.signer_factory import sdk_factory
        for deployment in ('hyperliquid-mainnet-native','lighter-core-mainnet'):
            with self.subTest(deployment=deployment),tempfile.TemporaryDirectory() as root:
                scope,key=spec(root,deployment)
                public=Mock();sdk=Mock()
                with self.assertRaisesRegex(Reject,'SDK hydration refused'):
                    sdk_factory(scope,'02'*(len(key)//2),public_factory=public,
                                exchange_factory=sdk,signer_factory=sdk)
                public.assert_not_called();sdk.assert_not_called()

    def test_native_wrong_account_agent_deployment_or_asset_fails_closed(self):
        from funding_collector.signer_factory import sdk_factory
        from hyperliquid.exchange import Exchange
        from funding_collector.hyperliquid import HyperliquidInstrument,resolve_contract
        for field,value in [('account_address','0x'+'33'*20),('base_url','https://api.hyperliquid-testnet.xyz'),
                            ('vault_address','0x'+'44'*20),('wallet',None),('asset',1)]:
            with self.subTest(field=field),tempfile.TemporaryDirectory() as root:
                scope,key=spec(root)
                public=Mock();public._metadata.return_value=(dict(native_spot(),universe=[]),native_meta(),time.time())
                def exchange(*args,**kwargs):
                    ex=Exchange(*args,**kwargs)
                    if field=='wallet':ex.wallet=Mock(address='0x'+'22'*20)
                    elif field=='asset':ex.info.coin_to_asset['ETH']=value
                    else:setattr(ex,field,value)
                    return ex
                with patch.object(socket.socket,'connect',side_effect=AssertionError('NO NETWORK')):
                    with self.assertRaisesRegex(Reject,'SDK hydration refused'):
                        sdk_factory(scope,key,public_factory=lambda *a:public,exchange_factory=exchange)

    def test_core_wrong_sdk_chain_account_key_index_fails_closed(self):
        from funding_collector.signer_factory import sdk_factory
        from funding_collector.lighter import normalize_market
        from types import SimpleNamespace as NS
        market=normalize_market(dict(symbol='ETH',market_id=0,market_type='perp',status='active',
                supported_size_decimals=4,supported_price_decimals=2,min_base_amount='.001',min_quote_amount='10'))
        for field,value in [('url','https://api.rh.lighter.xyz'),('chain_id',300),('account_index',124),
                            ('api_key_dict',{5:'01'*40})]:
            with self.subTest(field=field),tempfile.TemporaryDirectory() as root:
                scope,key=spec(root,'lighter-core-mainnet')
                public=Mock();public.snapshot.return_value=(market,book(),{})
                def signer(url,account,keys,**kw):
                    s=NS(url=url,account_index=account,api_key_dict=keys,chain_id=kw['chain_id'],api_client=Mock())
                    setattr(s,field,value);return s
                with self.assertRaisesRegex(Reject,'SDK hydration refused'):
                    sdk_factory(scope,key,public_factory=lambda *a:public,signer_factory=signer,account_factory=Mock())

    def test_read_sdk_wrong_native_map_refused(self):
        from funding_collector.signer_factory import sdk_factory
        from hyperliquid.info import Info
        with tempfile.TemporaryDirectory() as root:
            scope,key=spec(root)
            public=Mock();public._metadata.return_value=(dict(native_spot(),universe=[]),native_meta(),time.time())
            def info(*a,**kw):
                result=Info(*a,**kw);result.coin_to_asset['ETH']=10000;return result
            with self.assertRaisesRegex(Reject,'SDK hydration refused'):
                sdk_factory(scope,key,public_factory=lambda *a:public,info_factory=info)

    def test_registration_mismatch_never_reserves_or_signs(self):
        from funding_collector.signer_factory import _CorePrivate,_NativePrivate
        from types import SimpleNamespace as NS
        for cls in (_CorePrivate,_NativePrivate):
            with self.subTest(bridge=cls.__name__):
                scope,key=spec('/unused','lighter-core-mainnet' if cls is _CorePrivate else 'hyperliquid-mainnet-native')
                bridge=object.__new__(cls);bridge.identity=scope.identity;bridge.market=NS(symbol='ETH',market_id=0)
                bridge.authority=scope.authority();bridge.authority.arm('ARM EXACT SCOPED ORDERS',lifetime=10)
                bridge.signer=Mock();bridge.signer.check_client.return_value='mismatch'
                bridge.info=Mock();bridge.info.extra_agents.return_value=[]
                bridge.ledger=Mock();bridge.exchange=Mock()
                with self.assertRaises(Reject):
                    asyncio.run(bridge.submit(Order('0x'+'ab'*16,'buy','.02','3000','3000','LIMIT')))
                bridge.ledger.reserve.assert_not_called();bridge.signer.sign_create_order.assert_not_called()
                bridge.exchange.post.assert_not_called()
