"""Dummy keystore tests, encrypted at rest, no live signer activation."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from funding_collector.domain import Reject


class KeystoreTests(unittest.TestCase):
    def test_encrypted_scoped_key_masked_repr_expiry_and_lock(self):
        from funding_collector.keystore import KeyStore
        from funding_collector.live import Identity
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'vault'
            k=KeyStore(path)
            from eth_account import Account
            i=Identity('hyperliquid-mainnet-native','0x'+'11'*20,Account.from_key('0x'+'01'*32).address)
            dummy='01'*32;password='dummy-only-passphrase-2026'
            k.create(i,dummy,password)
            raw=(path/'key.json').read_bytes()
            self.assertNotIn(dummy.encode(),raw);self.assertNotIn(password.encode(),raw)
            self.assertEqual((path/'key.json').stat().st_mode&0o777,0o600)
            self.assertEqual(path.stat().st_mode&0o777,0o700)
            with self.assertRaises(Reject):k.unlock(i,'incorrect-passphrase',lifetime=1)
            with self.assertRaises(Reject):k.unlock(Identity(i.deployment,'0x'+'33'*20,i.signer),password,lifetime=1)
            k.unlock(i,password,lifetime=1)
            self.assertTrue(k.unlocked);self.assertNotIn(dummy,repr(k))
            time.sleep(1.1);self.assertFalse(k.unlocked)
            k.unlock(i,password,lifetime=30);k.lock();self.assertFalse(k.unlocked)
            with self.assertRaises(Reject):k.create(i,dummy,password)
            os.chmod(path/'key.json',0o644)
            with self.assertRaises(Reject):k.unlock(i,password,lifetime=1)

    def test_tamper_and_no_plaintext_or_symlink_fallback(self):
        from funding_collector.keystore import KeyStore
        from funding_collector.live import Identity
        with tempfile.TemporaryDirectory() as root:
            import hashlib
            i=Identity('lighter-core-mainnet','123',hashlib.sha256(bytes.fromhex('01'*40)).hexdigest(),4)
            k=KeyStore(Path(root)/'vault');k.create(i,'01'*40,'dummy-only-passphrase-2026')
            p=k.root/'key.json';raw=json.loads(p.read_text());raw['ciphertext']='00'+raw['ciphertext'][2:]
            p.write_text(json.dumps(raw))
            with self.assertRaises(Reject):k.unlock(i,'dummy-only-passphrase-2026',lifetime=30)
            p.unlink();p.symlink_to(Path(root)/'elsewhere')
            with self.assertRaises(Reject):k.unlock(i,'dummy-only-passphrase-2026',lifetime=30)
