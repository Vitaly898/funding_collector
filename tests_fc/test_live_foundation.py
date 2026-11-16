"""Offline contracts only; dummy keys and SDK transport fakes, no network."""
import asyncio
from decimal import Decimal as D
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from funding_collector.domain import Reject


class AuthorityTests(unittest.TestCase):
    def test_scoped_identity_and_expiring_runtime_arm(self):
        from funding_collector.live import Identity, Authority, Order
        i = Identity('hyperliquid-mainnet-native', '0x'+'11'*20, '0x'+'22'*20)
        with self.assertRaises(Reject): Identity('hyperliquid-mainnet-io', i.account, i.signer)
        a = Authority(i, 'ETH', 1, D('0.1'), D('500'), D('50'))
        o = Order('0x'+'ab'*16, 'buy', D('.02'), D('3000'), D('3000'), 'LIMIT')
        with self.assertRaises(Reject): a.check(i, 'ETH', 1, o)
        a.arm('ARM EXACT SCOPED ORDERS', lifetime=10)
        a.check(i, 'ETH', 1, o)
        with self.assertRaises(Reject): a.check(Identity(i.deployment, '0x'+'33'*20, i.signer), 'ETH', 1, o)
        with self.assertRaises(Reject): a.check(i, 'io:ETH', 1, o)
        with self.assertRaises(Reject): a.check(i, 'ETH', 1, Order(o.client_id,'buy',D('.02'),D('3100'),D('3000'),'MARKET'))
        a.lock()
        with self.assertRaises(Reject): a.check(i, 'ETH', 1, o)


class HyperliquidContractTests(unittest.TestCase):
    def test_real_sdk_builds_and_signs_native_order_with_dummy_key_ack_is_not_fill(self):
        from funding_collector.live import Identity, Authority, Order
        from funding_collector.live_hyperliquid import HyperliquidPrivate
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        from eth_account import Account
        wallet=Account.from_key('0x'+'01'*32)  # obvious test-only key
        account='0x'+'11'*20
        info=Info('https://api.hyperliquid.xyz',skip_ws=True,
                  meta={'universe':[{'name':'ETH','szDecimals':4}]},spot_meta={'universe':[],'tokens':[]})
        ex=Exchange(wallet,'https://api.hyperliquid.xyz',meta={'universe':[{'name':'ETH','szDecimals':4}]},
                    spot_meta={'universe':[],'tokens':[]},account_address=account)
        from funding_collector.domain import Instrument
        # The adapter must require the genuine native instrument, not generic assets.
        from funding_collector.hyperliquid import HyperliquidInstrument, resolve_contract
        from tests_fc.test_hyperliquid import native_meta,native_spot
        market=HyperliquidInstrument(resolve_contract(native_meta(),native_spot(),'ETH'))
        i=Identity('hyperliquid-mainnet-native',account,wallet.address)
        a=Authority(i,'ETH',0,D('.1'),D('500'),D('50'));a.arm('ARM EXACT SCOPED ORDERS',lifetime=10)
        with __import__('tempfile').TemporaryDirectory() as root:
            bridge=HyperliquidPrivate(ex,info,i,market,a,root)
            with patch.object(ex,'post',return_value={'status':'ok','response':{'type':'order','data':{'statuses':[{'resting':{'oid':7}}]}}}) as post:
                ack=asyncio.run(bridge.submit(Order('0x'+'ab'*16,'buy',D('.02'),D('3000'),D('3000'),'LIMIT')))
                self.assertEqual(ack['status'],'ACK_ONLY');self.assertIsNone(ack['filled_base'])
                route,payload=post.call_args.args
                self.assertEqual(route,'/exchange')
                w=payload['action']['orders'][0]
                self.assertEqual(w,{'a':0,'b':True,'p':'3000','s':'0.02','r':False,'t':{'limit':{'tif':'Alo'}},'c':'0x'+'ab'*16})
                self.assertNotIn('builder',payload['action']);self.assertEqual(set(payload['signature']),{'r','s','v'})
                self.assertGreater(payload['nonce'],0)
                with self.assertRaises(Reject):asyncio.run(bridge.submit(Order('0x'+'ab'*16,'buy',D('.02'),D('3000'),D('3000'),'LIMIT')))
                self.assertEqual(post.call_count,1)
            with patch.object(info,'post',side_effect=[{'assetPositions':[]},[],{'status':'unknownOid'},[]]) as post:
                self.assertEqual(asyncio.run(bridge.positions()),[])
                self.assertEqual(asyncio.run(bridge.open_orders()),[])
                self.assertEqual(asyncio.run(bridge.order_status('0x'+'ab'*16))['status'],'UNKNOWN')
                self.assertEqual(asyncio.run(bridge.fills(7)),[])
                self.assertTrue(all(c.args[1]['user']==account for c in post.call_args_list))
            bridge.close()
