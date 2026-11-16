"""Offline private venue expansion; dummy keys, network forbidden."""
import asyncio
import json
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
from funding_collector.domain import Reject
from funding_collector.keystore import _binding
from funding_collector.live import Order
from funding_collector.signer_session import SessionSpec
from tests_fc.test_signer_factory import book


class PairExpansionTests(unittest.TestCase):
    def test_core_rh_config_and_worker_reuse_existing_engine(self):
        from dataclasses import asdict
        from funding_collector.supervised import load_config
        from funding_collector.live_worker import Worker
        from tests_fc.test_supervised import config
        from tests_fc.test_live_worker import pair, intent
        with tempfile.TemporaryDirectory() as root:
            c=config()
            c['legs'][0]['identity']=asdict(_binding('lighter-rh-mainnet','124',5,'02'*40))
            path=Path(root)/'c.json';path.write_text(json.dumps(c))
            loaded=load_config(path,root)
            self.assertEqual(loaded.specs[0].identity.deployment,'lighter-rh-mainnet')
            p,h=pair();p.identity=loaded.specs[0].identity
            with Worker(Path(root)/'worker',p,h) as worker:
                worker.add(intent())
                self.assertEqual(worker.state(intent()['id'])['phase'],'BASELINE')

    def test_same_execution_key_cannot_cross_deployments(self):
        from dataclasses import asdict
        from funding_collector.supervised import load_config
        from tests_fc.test_supervised import config
        with tempfile.TemporaryDirectory() as root:
            c=config()
            c['legs'][0]['identity']=asdict(_binding('lighter-rh-mainnet','124',5,'01'*40))
            path=Path(root)/'c.json';path.write_text(json.dumps(c))
            with self.assertRaisesRegex(Reject,'distinct dedicated execution keys'):
                load_config(path,root)


class RHPrivateTests(unittest.TestCase):
    def test_rh_factory_real_chain_signing_and_unknown_freeze(self):
        import lighter
        from funding_collector.signer_factory import sdk_factory
        from funding_collector.lighter import normalize_market
        async def run(root):
            key='02'*40
            identity=_binding('lighter-rh-mainnet','123',4,key)
            scope=SessionSpec(identity,'ETH',0,'.1','500','50',str(Path(root)/'j'),str(Path(root)/'p'))
            market=normalize_market(dict(symbol='ETH',market_id=0,market_type='perp',status='active',
                supported_size_decimals=4,supported_price_decimals=2,min_base_amount='.001',min_quote_amount='10'))
            public=Mock();public.snapshot.return_value=(market,book(),{})
            with patch.object(socket.socket,'connect',side_effect=AssertionError('NO NETWORK')):
                bridge,_=sdk_factory(scope,key,public_factory=lambda deployment,*a: public)
                s=bridge.signer
                self.assertEqual((s.url,s.chain_id),('https://api.rh.lighter.xyz',466324))
                self.assertEqual(bridge.identity.deployment,'lighter-rh-mainnet')
                bridge.authority.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
                s.check_client=Mock(return_value=None)
                s.tx_api.next_nonce=AsyncMock(return_value=NS(code=200,nonce=1))
                s.tx_api.send_tx=AsyncMock(side_effect=TimeoutError())
                original=s.sign_create_order; signed=[]
                def sign(**kw):
                    out=original(**kw);self.assertIsNone(out[3]);signed.append(json.loads(out[1]));return out
                with patch.object(s,'sign_create_order',side_effect=sign):
                    ack=await bridge.submit(Order('0x'+'ab'*16,'sell','.02','2990','3000','MARKET'))
                    self.assertEqual(ack['status'],'UNKNOWN')
                    self.assertIsNone(ack['filled_base'])
                    s.tx_api.next_nonce.return_value=NS(code=200,nonce=2)
                    with self.assertRaises(Reject):
                        await bridge.submit(Order('0x'+'cd'*16,'sell','.02','2990','3000','MARKET'))
                self.assertEqual(len(signed),1)
                self.assertEqual((signed[0]['BaseAmount'],signed[0]['Price'],signed[0]['AccountIndex']),(200,299000,123))
                bridge.close();await s.close()
        with tempfile.TemporaryDirectory() as root:asyncio.run(run(root))
