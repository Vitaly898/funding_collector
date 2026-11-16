"""Additional fail-closed tests without SDK/network."""
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from funding_collector.domain import Reject
from funding_collector.live import Identity,Order,OrderLedger


class LedgerSafetyTests(unittest.TestCase):
    def test_unknown_pending_blocks_other_ids_and_nonce_reuse(self):
        identity=Identity('lighter-core-mainnet','123','dummy',4)
        a=Order('0x'+'ab'*16,'buy',D('.02'),D('3000'),D('3000'),'LIMIT')
        b=Order('0x'+'cd'*16,'buy',D('.02'),D('3000'),D('3000'),'LIMIT')
        with tempfile.TemporaryDirectory() as root:
            l=OrderLedger(root,identity)
            self.assertEqual(l.reserve(a,0),0)
            with self.assertRaises(Reject):l.reserve(b,1)
            l.status(a.client_id,'UNKNOWN')
            with self.assertRaises(Reject):l.reserve(b,1)
            l.status(a.client_id,'CONFIRMED')
            with self.assertRaises(Reject):l.reserve(b,0)
            self.assertEqual(l.reserve(b,1),1)
            l.close()

    def test_exclusive_writer_and_wrong_scope_refused_after_restart(self):
        i=Identity('lighter-core-mainnet','123','dummy',4)
        with tempfile.TemporaryDirectory() as root:
            l=OrderLedger(root,i)
            with self.assertRaises(Reject):OrderLedger(root,i)
            l.close()
            with self.assertRaises(Reject):OrderLedger(root,Identity(i.deployment,'124',i.signer,4))
