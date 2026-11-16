"""Shared Extended CLI path: dummy key 1 and synthetic transports only."""
import asyncio
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch
from funding_collector.domain import Reject
from funding_collector.signer_session import SessionSpec, validate_snapshot
from funding_collector.live import Order
from tests_fc.test_extended_integration import bundle, identity, PASSWORD
from tests_fc.test_extended_private_worker import sdk_market, HTTP
from tests_fc.test_signer_factory import book


def spec(root):
    return SessionSpec(identity(),'ETH',2,'.1','500','50',str(Path(root)/'journal'),
                       str(Path(root)/'public'),fee_rate_cap='.0005')


class OfflinePublic:
    def __init__(self, artifacts): pass
    def snapshot(self, symbol):
        from funding_collector.extended import normalize_market
        if symbol!='ETH-USD': raise AssertionError('wrong wire symbol')
        return normalize_market(sdk_market()),book(),{}


def offline_extended_factory(scope, credentials):
    from funding_collector.signer_factory import sdk_factory
    from tests_fc.test_extended_private_worker import trade_row
    from funding_collector.extended_private import ExtendedCredentials
    if not isinstance(credentials,ExtendedCredentials): raise AssertionError('typed bundle missing')
    if len(credentials.api_key)<=256: raise AssertionError('pipe boundary not exercised')
    http=HTTP()
    http.routes['GET','user/positions']={'status':'OK','data':[]}
    http.routes['GET','user/orders']={'status':'OK','data':[]}
    http.routes['GET','user/trades']={'status':'OK','data':[trade_row()]}
    with patch.object(socket.socket,'connect',side_effect=AssertionError('NO NETWORK')):
        return sdk_factory(scope,credentials,public_factory=OfflinePublic,http_session_factory=lambda:http)


class SessionTests(unittest.TestCase):
    def test_typed_bundle_child_readonly_registration_without_order_authority(self):
        from funding_collector.keystore import KeyStore
        from funding_collector.signer_session import SignerSession
        with tempfile.TemporaryDirectory() as root:
            s=spec(root); vault=KeyStore(Path(root)/'vault')
            vault.create(s.identity,replace(bundle(),api_key='dummy-'+'x'*400),PASSWORD)
            with SignerSession(vault,s,factory=offline_extended_factory) as session:
                session.unlock_readonly(PASSWORD,lifetime=30)
                self.assertFalse(vault.unlocked)
                self.assertEqual(session.authority._until,0)
                self.assertTrue(asyncio.run(session.registration()))
                self.assertEqual(asyncio.run(session.positions()),[])
                self.assertEqual(asyncio.run(session.open_orders()),[])
                with self.assertRaises(Reject):
                    asyncio.run(session.submit(Order('0x'+'ab'*16,'buy','.02','3000','3000','LIMIT')))
                self.assertTrue(session.alive)
                with self.assertRaises(Reject): session._rpc('fills',42)
                self.assertFalse(session.alive)

    def test_armed_session_preserves_verified_usd_fills(self):
        from funding_collector.keystore import KeyStore
        from funding_collector.signer_session import SignerSession, ACK
        with tempfile.TemporaryDirectory() as root:
            s=spec(root); vault=KeyStore(Path(root)/'vault')
            vault.create(s.identity,replace(bundle(),api_key='dummy-'+'x'*400),PASSWORD)
            with SignerSession(vault,s,factory=offline_extended_factory) as session:
                session.unlock(PASSWORD,confirmation=ACK,lifetime=30)
                asyncio.run(session.registration())
                rows=asyncio.run(session.fills(42))
                self.assertEqual(rows[0]['fee_currency'],'USD')
                self.assertEqual(rows[0]['quantity'],'0.01')


class FactoryTests(unittest.TestCase):
    def test_default_extended_locked_offline_constructor_exact_mapping(self):
        from funding_collector.extended import normalize_market
        from funding_collector.extended_private import ExtendedPrivate
        from funding_collector.signer_factory import sdk_factory
        async def run(root):
            public=Mock(); http=HTTP()
            public.snapshot.side_effect=lambda symbol:(normalize_market(sdk_market()),book(),{})
            with patch.object(socket.socket,'connect',side_effect=AssertionError('NO NETWORK')):
                bridge,snapshot=sdk_factory(spec(root),bundle(),public_factory=lambda *a:public,
                                            http_session_factory=lambda:http)
                try:
                    self.assertIsInstance(bridge,ExtendedPrivate)
                    self.assertEqual(bridge.authority._until,0)
                    self.assertEqual(http.calls,[])
                    self.assertEqual((bridge.market.symbol,bridge.market.market_id),('ETH',2))
                    self.assertEqual(bridge.instrument.symbol,'ETH-USD')
                    self.assertEqual(bridge.fee_rate,D('.0005'))
                    sample=snapshot()
                    self.assertEqual(sample[0].raw,sdk_market())
                    validate_snapshot(bridge,sample,Order('0x'+'ab'*16,'buy','.02','3000','3000','LIMIT'))
                    self.assertTrue(all(c.args==('ETH-USD',) for c in public.snapshot.call_args_list))
                    await bridge.registration()
                    self.assertEqual(bridge.authority._until,0)
                    with self.assertRaises(Reject):
                        await bridge.submit(Order('0x'+'ab'*16,'buy','.02','3000','3000','LIMIT'))
                    self.assertTrue(all(c[0]=='GET' for c in http.calls))
                finally: bridge.close()
        with tempfile.TemporaryDirectory() as root: asyncio.run(run(root))

    def test_explicit_fee_cap_required_only_for_extended(self):
        with tempfile.TemporaryDirectory() as root:
            s=spec(root)
            for cap in (None,'-1','NaN','1.01'):
                with self.subTest(cap=cap),self.assertRaises(Reject): replace(s,fee_rate_cap=cap)
            self.assertEqual(replace(s,fee_rate_cap='0').fee_rate_cap,'0')
