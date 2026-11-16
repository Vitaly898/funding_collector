"""Subprocess custody tests: obvious dummy key, injected offline factory only."""
import asyncio
from dataclasses import asdict
from decimal import Decimal as D
import os
from pathlib import Path
import tempfile
import time
import unittest

from funding_collector.domain import Reject, Book, Instrument
from funding_collector.keystore import KeyStore, _binding
from funding_collector.live import Order, Authority

DUMMY='01'*40
PASSWORD='dummy-only-passphrase-2026'


class DummyBridge:
    def __init__(self, spec, secret):
        if secret != DUMMY: raise ValueError('SECRET_SENTINEL_'+secret)
        self.identity=spec.identity
        self.market=Instrument('ETH',0,'ETH','USD','linear_perpetual','base',1,True,'.0001','.01','.001','10')
        self.authority=spec.authority()
    async def positions(self): return []
    async def open_orders(self): return []
    async def submit(self, order):
        self.authority.check(self.identity,'ETH',0,order)
        return dict(status='ACK_ONLY',filled_base=None)
    async def order_status(self, client_id): raise RuntimeError('SECRET_SENTINEL_'+DUMMY)
    async def fills(self, oid): return []


def dummy_factory(spec, secret):
    b=DummyBridge(spec,secret)
    def snapshot():
        return b.market,Book(dict(bids=[dict(price='2999',remaining_base_amount='1')],
                                  asks=[dict(price='3001',remaining_base_amount='1')]),time.time()),{}
    return b,snapshot


class SessionTests(unittest.TestCase):
    def vault(self, root):
        k=KeyStore(Path(root)/'vault');i=_binding('lighter-core-mainnet','123',4,DUMMY)
        k.create(i,DUMMY,PASSWORD)
        return k,i
    def spec(self, root, identity):
        from funding_collector.signer_session import SessionSpec
        return SessionSpec(identity,'ETH',0,'.1','500','50',str(Path(root)/'journal'),str(Path(root)/'public'))
    def test_unlock_hydrates_child_only_and_lock_kills_authority(self):
        from funding_collector.signer_session import SignerSession
        with tempfile.TemporaryDirectory() as root:
            k,i=self.vault(root)
            with SignerSession(k,self.spec(root,i),factory=dummy_factory) as s:
                with self.assertRaises(Reject):s.unlock(PASSWORD,confirmation='no',lifetime=20)
                self.assertFalse(k.unlocked)
                s.unlock(PASSWORD,confirmation='ARM EXACT SCOPED ORDERS',lifetime=20)
                self.assertNotEqual(s.pid,os.getpid());self.assertTrue(s.alive)
                self.assertFalse(k.unlocked)  # parent vault purged after handoff
                o=Order('0x'+'ab'*16,'buy',D('.02'),D('3000'),D('3000'),'LIMIT')
                self.assertEqual(asyncio.run(s.submit(o))['status'],'ACK_ONLY')
                self.assertNotIn(DUMMY,repr(s));self.assertNotIn(PASSWORD,repr(s))
                with self.assertRaisesRegex(Reject,'signer operation refused'):
                    asyncio.run(s.order_status(o.client_id))
                k.lock();self.assertFalse(s.alive)
                with self.assertRaises(Reject):asyncio.run(s.submit(o))
    def test_guard_rejects_stale_reference_metadata_depth_and_crossing(self):
        from funding_collector.signer_session import validate_snapshot
        from types import SimpleNamespace
        i=_binding('lighter-core-mainnet','123',4,DUMMY)
        with tempfile.TemporaryDirectory() as root:
            b,snapshot=dummy_factory(self.spec(root,i),DUMMY)
            def order(side='buy',price='3000',reference='3000',kind='LIMIT',qty='.02'):
                return Order('0x'+'ab'*16,side,D(qty),D(price),D(reference),kind)
            validate_snapshot(b,snapshot(),order())
            validate_snapshot(b,snapshot(),order('sell','2999','3000','MARKET'))
            cases=[(snapshot(),order(price='3001')),(snapshot(),order(reference='2900')),
                   (snapshot(),order(price='3000',kind='MARKET')),(snapshot(),order(qty='2'))]
            m,book,meta=snapshot();book.observed_at-=4;cases.append(((m,book,meta),order()))
            m,book,meta=snapshot();changed=Instrument('ETH',1,'ETH','USD','linear_perpetual','base',1,True,'.0001','.01','.001','10')
            cases.append(((changed,book,meta),order()))
            for sample,o in cases:
                with self.subTest(order=o),self.assertRaises(Reject):validate_snapshot(b,sample,o)

    def test_expiry_kills_process_without_poll_and_wrong_password_identity_refused(self):
        from funding_collector.signer_session import SignerSession
        from funding_collector.live import Identity
        with tempfile.TemporaryDirectory() as root:
            k,i=self.vault(root)
            with SignerSession(k,self.spec(root,i),factory=dummy_factory) as s:
                with self.assertRaises(Reject):s.unlock('wrong-password-000',confirmation='ARM EXACT SCOPED ORDERS',lifetime=2)
                self.assertFalse(s.alive)
                s.unlock(PASSWORD,confirmation='ARM EXACT SCOPED ORDERS',lifetime=2)
                pid=s.pid;time.sleep(2.3)
                self.assertFalse(s.alive)
                with self.assertRaises(ProcessLookupError):os.kill(pid,0)
            wrong=Identity(i.deployment,'124',i.signer,4)
            with SignerSession(k,self.spec(root,wrong),factory=dummy_factory) as s:
                with self.assertRaises(Reject):s.unlock(PASSWORD,confirmation='ARM EXACT SCOPED ORDERS',lifetime=10)
                self.assertFalse(s.alive)
