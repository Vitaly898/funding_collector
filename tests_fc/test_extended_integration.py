"""Extended shared path: synthetic accounts, dummy Stark key 1; no network."""
import asyncio
from dataclasses import asdict, replace
from decimal import Decimal as D
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from funding_collector.domain import Reject
from funding_collector.live import Identity
from funding_collector.keystore import KeyStore, _binding
from funding_collector.extended_private import ExtendedCredentials
from tests_fc.test_extended_private_worker import account_row, sdk_market, HTTP

PASSWORD='dummy-only-extended-password'
DEP='extended-mainnet-starknet'

def bundle():
    return ExtendedCredentials('dummy-api-only', '0x1', account_row()['l2Key'], 7)

def identity():
    return Identity(DEP,'23',account_row()['l2Key'])

class CustodyTests(unittest.TestCase):
    def test_bundle_encrypted_authenticated_and_typed_handoff(self):
        from funding_collector.keystore import _decode_secret
        c=bundle(); i=identity()
        self.assertEqual(_binding(DEP,'23',None,c),i)
        with tempfile.TemporaryDirectory() as root:
            k=KeyStore(root); k.create(i,c,PASSWORD)
            raw=Path(root,'key.json').read_text()
            self.assertNotIn(c.api_key,raw); self.assertNotIn(c.stark_private_key+'"',raw)
            k.unlock(i,PASSWORD,lifetime=30)
            class Pipe:
                def send_bytes(self, value): self.value=bytes(value)
            pipe=Pipe(); k.handoff(i,pipe,lambda:None)
            self.assertEqual(_decode_secret(i,pipe.value),c)
            self.assertFalse(k.unlocked); k.lock()
            with self.assertRaises(Reject): k.unlock(replace(i,account='24'),PASSWORD,lifetime=1)
            data=json.loads(raw); data['identity']['account']='24'
            Path(root,'key.json').write_text(json.dumps(data))
            with self.assertRaises(Reject): k.unlock(replace(i,account='24'),PASSWORD,lifetime=1)
    def test_identity_and_bounded_typed_pipe_reject_wrong_kind_or_scope(self):
        from funding_collector.keystore import _decode_secret, _encode_secret
        i=identity(); encoded=_encode_secret(i,bundle())
        self.assertEqual(_decode_secret(i,encoded),bundle())
        for bad in (b'x'*8193,b'{}',b'"0x1"',encoded.replace(b'extended-credentials-v1',b'other-credentials-v001')):
            with self.subTest(length=len(bad)),self.assertRaises(Reject): _decode_secret(i,bad)
        for changes in (dict(account='023'),dict(signer='not-stark'),dict(api_key_index=4)):
            with self.subTest(changes=changes),self.assertRaises(Reject): replace(i,**changes)
