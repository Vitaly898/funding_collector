"""Offline release boundary regressions; never load user credentials."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch
from funding_collector.domain import Reject


class ReadOnlyTests(unittest.TestCase):
    def test_readonly_session_never_arms_and_child_refuses_submit(self):
        from funding_collector.signer_session import SignerSession
        from funding_collector.keystore import KeyStore
        from tests_fc.test_signer_session import dummy_factory, SessionTests, PASSWORD
        from funding_collector.live import Order
        with tempfile.TemporaryDirectory() as root:
            v,identity=SessionTests().vault(root)
            scope=SessionTests().spec(root,identity)
            with SignerSession(v,scope,factory=dummy_factory) as session:
                session.unlock_readonly(PASSWORD,lifetime=15)
                self.assertEqual(session.authority._until,0)
                self.assertEqual(asyncio.run(session.positions()),[])
                with self.assertRaises(Reject):
                    session._rpc('submit',dict(client_id='0x'+'ab'*16,side='buy',quantity='.02',price='3000',reference_price='3000',kind='LIMIT'))

    def test_account_check_nonterminal_refused_and_no_arming_options(self):
        from tests_fc.test_supervised import CommandTests,config
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'config.json';p.write_text(json.dumps(config()))
            r=CommandTests().invoke('account-check','--config',str(p),'--root',str(Path(root)/'absent'))
            self.assertNotEqual(r.returncode,0)
            self.assertFalse((Path(root)/'absent').exists())
            h=CommandTests().invoke('account-check','--help')
            self.assertEqual(h.returncode,0)
            self.assertNotIn('--arm',h.stdout)
            self.assertNotIn('--password',h.stdout)


class SDKCharacterization(unittest.TestCase):
    def test_lighter_minus_one_is_expanded_by_native_signer_not_sent_negative(self):
        import lighter,time
        async def run():
            s=lighter.SignerClient('https://mainnet.zklighter.elliot.ai',123,{4:'01'*40},nonce_management_type=lighter.nonce_manager.NonceManagerType.NONE,chain_id=304)
            try:
                before=int(time.time()*1000)
                out=s.sign_create_order(0,1,200,300000,False,0,2,order_expiry=-1,nonce=1,api_key_index=4)
                self.assertIsNone(out[3])
                tx=json.loads(out[1])
                self.assertLess(abs(tx['OrderExpiry']-before-28*86400*1000),5000)
                self.assertEqual(tx.get('IntegratorTakerFee',0),0)
                self.assertEqual(tx.get('IntegratorMakerFee',0),0)
            finally:await s.close()
        with patch.object(socket.socket,'connect',side_effect=AssertionError('NO NETWORK')):asyncio.run(run())
