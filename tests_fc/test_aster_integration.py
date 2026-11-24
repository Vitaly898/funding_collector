"""Aster custody and normal supervised entrypoints, no private network."""
import tempfile,unittest,time,socket
from pathlib import Path
from unittest.mock import patch,Mock
from dataclasses import replace
from funding_collector.domain import Reject,Book
from funding_collector.keystore import _binding,_encode_secret,_decode_secret
from funding_collector.live import Identity,validate_private_pair
from funding_collector.signer_session import SessionSpec
from tests_fc.test_aster import metadata

KEY='01'*32
OWNER='0x'+'11'*20

def scope(root):
 identity=_binding('aster-mainnet',OWNER,None,KEY)
 return SessionSpec(identity,'ETH',1,'.1','500','50',str(Path(root)/'journal'),str(Path(root)/'public'))

class IntegrationTests(unittest.TestCase):
 def test_identity_custody_and_pair_scope(self):
  with tempfile.TemporaryDirectory() as root:
   spec=scope(root)
   self.assertEqual(_decode_secret(spec.identity,_encode_secret(spec.identity,KEY)),KEY)
   spec.authority()
   with self.assertRaises(Reject):Identity('aster-mainnet',spec.identity.signer,spec.identity.signer)
   with self.assertRaises(Reject):Identity('aster-mainnet',OWNER,spec.identity.signer,4)
   counter=SessionSpec(_binding('lighter-core-mainnet','123',4,'02'*40),'ETH',0,'.1','500','50',root,root)
   validate_private_pair(spec,counter)
   with self.assertRaises(Reject):validate_private_pair(spec,replace(counter,symbol='BTC'))
   with self.assertRaises(Reject):validate_private_pair(spec,replace(spec,identity=_binding('aster-mainnet','0x'+'22'*20,None,'02'*32)))

 def test_default_factories_hydrate_aster_and_hyperliquid_without_private_io(self):
  from funding_collector.signer_factory import sdk_factory
  from funding_collector.aster import AsterInstrument
  from funding_collector.live_aster import AsterPrivate
  from tests_fc.test_live_aster import Wire
  from tests_fc.test_hyperliquid import native_meta,native_spot
  from funding_collector.hyperliquid import HyperliquidInstrument,resolve_contract
  with tempfile.TemporaryDirectory() as root,patch.object(socket.socket,'connect',side_effect=AssertionError('NO NETWORK')):
   spec=scope(root)
   public=Mock()
   public.snapshot.side_effect=lambda symbol:(AsterInstrument(metadata(),'2000'),Book(dict(bids=[dict(price='1999.99',remaining_base_amount='2')],asks=[dict(price='2000.01',remaining_base_amount='2')]),time.time()),{})
   bridge,snapshot=sdk_factory(spec,KEY,public_factory=lambda _:public,aster_transport=Wire())
   self.addCleanup(bridge.close)
   self.assertIsInstance(bridge,AsterPrivate)
   self.assertEqual(bridge.authority._until,0)
   self.assertEqual(snapshot()[0].symbol,'ETH')
   hlkey='02'*32
   hlspec=SessionSpec(_binding('hyperliquid-mainnet-native','0x'+'22'*20,None,hlkey),'ETH',0,'.1','500','50',str(Path(root)/'hl-journal'),str(Path(root)/'hl-public'))
   hlpublic=Mock();hlpublic._metadata.return_value=(dict(native_spot(),universe=[]),native_meta(),time.time())
   hlpublic.snapshot.side_effect=lambda symbol:(HyperliquidInstrument(resolve_contract(native_meta(),native_spot(),'ETH')),snapshot()[1],{})
   other,_=sdk_factory(hlspec,hlkey,public_factory=lambda _:hlpublic)
   self.addCleanup(other.close)
   validate_private_pair(spec,hlspec)
   self.assertEqual(other.market.base_asset,bridge.market.base_asset)

 def test_public_snapshot_maps_base_to_exact_wire_without_private_calls(self):
  from funding_collector.aster import AsterInstrument,AsterPublicAdapter
  from funding_collector.supervised import public_snapshot,validate_public,Config
  with tempfile.TemporaryDirectory() as root,patch.object(socket.socket,'connect',side_effect=AssertionError('NO NETWORK')):
   spec=scope(root)
   def sample(symbol):
    self.assertEqual(symbol,'ETHUSDT')
    return AsterInstrument(metadata(),'2000'),Book(dict(bids=[dict(price='1999.99',remaining_base_amount='2')],asks=[dict(price='2000.01',remaining_base_amount='2')]),time.time()),{}
   with patch.object(AsterPublicAdapter,'snapshot',side_effect=sample):
    market,book,source=public_snapshot(spec)
    self.assertEqual((market.symbol,market.base_asset,market.market_id,market.raw['symbol']),('ETH','ETH',1,'ETHUSDT'))

class AsterWorkerTests(unittest.IsolatedAsyncioTestCase):
 async def test_shared_worker_keeps_aster_usdt_fees_separate_and_repeat_idempotent(self):
  from tests_fc.test_live_worker import FakeVenue,pair,intent
  from funding_collector.live_worker import Worker
  from decimal import Decimal
  with tempfile.TemporaryDirectory() as root:
   primary=FakeVenue(scope(root).identity,1);hedge=pair()[1]
   original_fills=primary.fills
   async def usdt_fills(order_id):
    return [dict(r,fee_currency='USDT') for r in await original_fills(order_id)]
   primary.fills=usdt_fills
   with Worker(Path(root)/'worker',primary,hedge) as worker:
    order=intent();worker.add(order);worker.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
    await worker.tick(order['id'])
    primary.fraction=Decimal(1);primary.result='filled'
    await worker.tick(order['id'])
    hedge.fraction=Decimal(1);hedge.result='filled'
    await worker.tick(order['id'])
    result=worker.state(order['id'])
    self.assertEqual(result['status'],'COMPLETED_CONFIRMED')
    self.assertEqual(Decimal(result['residual_base']),0)
    self.assertEqual(result['fees_by_currency'],{'USDT':'0.02','USDC':'0.02'})
    before=worker.bytes(order['id']);await worker.tick(order['id'])
    self.assertEqual(worker.bytes(order['id']),before)
    self.assertEqual(len(primary.calls),1);self.assertEqual(len(hedge.calls),1)

if __name__=='__main__':unittest.main()
