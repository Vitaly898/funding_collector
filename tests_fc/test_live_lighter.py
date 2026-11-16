"""Offline real Lighter SDK signer contracts. Obvious dummy key only."""
import asyncio
import hashlib
import json
import socket
import tempfile
import unittest
from decimal import Decimal as D
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
from funding_collector.domain import Reject


class LighterPrivateTests(unittest.TestCase):
    def test_real_signer_integer_units_postonly_market_protection_and_durable_id_mapping(self):
        import lighter
        from funding_collector.live import Identity,Authority,Order
        from funding_collector.live_lighter import LighterPrivate
        from funding_collector.lighter import normalize_market
        async def run(root):
            key='01'*40  # non-secret obvious local test key, never submit
            s=lighter.SignerClient('https://mainnet.zklighter.elliot.ai',123,{4:key},
                    nonce_management_type=lighter.nonce_manager.NonceManagerType.NONE,chain_id=304)
            i=Identity('lighter-core-mainnet','123',hashlib.sha256(bytes.fromhex(key)).hexdigest(),4)
            a=Authority(i,'ETH',0,D('.1'),D('500'),D('50'));a.arm('ARM EXACT SCOPED ORDERS',lifetime=10)
            m=normalize_market(dict(symbol='ETH',market_id=0,market_type='perp',status='active',
                     supported_size_decimals=4,supported_price_decimals=2,min_base_amount='.001',min_quote_amount='10'))
            acc=NS(account=AsyncMock(return_value=NS(code=200,accounts=[NS(account_index=123,positions=[])])))
            b=LighterPrivate(s,acc,i,m,a,root)
            s.tx_api.next_nonce=AsyncMock(return_value=NS(nonce=1,code=200))
            s.tx_api.send_tx=AsyncMock(return_value=NS(code=200,tx_hash='dummy-response-hash'))
            signed=[]
            original=s.sign_create_order
            def sign(**kw):
                out=original(**kw)
                self.assertIsNone(out[3]);signed.append((kw,json.loads(out[1])))
                return out
            with patch.object(s,'sign_create_order',side_effect=sign):
                ack=await b.submit(Order('0x'+'ab'*16,'buy',D('.02'),D('3000'),D('3000'),'LIMIT'))
                self.assertEqual(ack['status'],'ACK_ONLY');self.assertIsNone(ack['filled_base'])
                with self.assertRaises(Reject):await b.submit(Order('0x'+'ab'*16,'buy',D('.02'),D('3000'),D('3000'),'LIMIT'))
                s.tx_api.next_nonce.return_value=NS(nonce=2,code=200)
                await b.submit(Order('0x'+'cd'*16,'sell',D('.02'),D('2990'),D('3000'),'MARKET'))
            k,tx=signed[0]
            self.assertEqual((k['base_amount'],k['price'],k['time_in_force'],k['order_type']),(200,300000,2,0))
            self.assertEqual((k['nonce'],k['api_key_index'],k['integrator_account_index']),(1,4,0))
            self.assertEqual(tx['AccountIndex'],123);self.assertEqual(tx['ApiKeyIndex'],4)
            self.assertEqual(tx['BaseAmount'],200);self.assertEqual(tx['ClientOrderIndex'],ack['client_order_index'])
            k,_=signed[1];self.assertEqual((k['order_type'],k['time_in_force'],k['order_expiry'],k['price']),(1,0,0,299000))
            self.assertNotEqual(signed[0][0]['client_order_index'],k['client_order_index'])
            s.order_api.account_active_orders=AsyncMock(return_value=NS(code=200,orders=[]))
            s.order_api.account_inactive_orders=AsyncMock(return_value=NS(code=200,orders=[],next_cursor=None))
            self.assertEqual(await b.positions(),[])
            self.assertEqual(await b.open_orders(),[])
            self.assertEqual((await b.order_status('0x'+'ab'*16))['status'],'UNKNOWN')
            s.order_api.trades=AsyncMock(return_value=NS(code=200,next_cursor=None,trades=[NS(
                trade_id=9,market_id=0,ask_account_id=123,bid_account_id=321,ask_id=7,bid_id=8,
                is_maker_ask=False,size='.02',price='3000',usd_amount='60',taker_fee=200,maker_fee=0,
                integrator_taker_fee=0,integrator_maker_fee=0)]))
            fills=await b.fills(7)
            self.assertEqual(fills[0]['fee'],'0.012')
            b.close();await s.close()
        with tempfile.TemporaryDirectory() as root,patch.object(socket.socket,'connect',side_effect=AssertionError('NO NETWORK')):
            asyncio.run(run(root))
