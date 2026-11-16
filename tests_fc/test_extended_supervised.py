"""Offline Extended supervisor and mixed-currency worker integration."""
import asyncio
from dataclasses import asdict
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest
from tests_fc.test_extended_integration import identity
from tests_fc.test_supervised import config
from tests_fc.test_live_worker import FakeVenue, pair, intent

class ExtendedVenue(FakeVenue):
    async def fills(self, oid):
        return [dict(r,fee_currency='USD') for r in await super().fills(oid)]

class IntegrationTests(unittest.TestCase):
    def test_public_config_requires_explicit_extended_fee_cap(self):
        from funding_collector.supervised import load_config
        from funding_collector.domain import Reject
        with tempfile.TemporaryDirectory() as root:
            c=config();c['legs'][0]['identity']=asdict(identity());c['legs'][0]['fee_rate_cap']='.0005'
            p=Path(root)/'config.json';p.write_text(json.dumps(c))
            self.assertEqual(load_config(p,Path(root)/'private').specs[0].fee_rate_cap,'.0005')
            del c['legs'][0]['fee_rate_cap'];p.write_text(json.dumps(c))
            with self.assertRaises(Reject):load_config(p,root)

    def test_masked_extended_setup_collects_typed_bundle(self):
        from funding_collector.supervised import setup_secret
        from funding_collector.extended_private import ExtendedCredentials
        from tests_fc.test_extended_integration import bundle
        from unittest.mock import Mock
        b=bundle();masked=Mock(side_effect=[b.api_key,b.stark_private_key,b.stark_public_key,str(b.vault)])
        self.assertEqual(setup_secret(identity(),masked),b)
        self.assertEqual(masked.call_count,4)
        self.assertIsInstance(b,ExtendedCredentials)

    def test_public_snapshot_extended_uses_base_to_wire_mapping(self):
        from funding_collector.supervised import public_snapshot
        from funding_collector.extended import ExtendedPublicAdapter
        from tests_fc.test_extended_default import spec
        from tests_fc.test_extended_private_worker import sdk_market
        from tests_fc.test_signer_factory import book
        from funding_collector.extended import normalize_market
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root,patch.object(ExtendedPublicAdapter,'snapshot',return_value=(normalize_market(sdk_market()),book(),{})) as snapshot:
            market,_,_=public_snapshot(spec(root))
            snapshot.assert_called_once_with('ETH-USD')
            self.assertEqual((market.symbol,market.market_id),('ETH',2))

    def test_full_limit_then_one_hedge_tracks_usd_and_usdc_separately(self):
        from funding_collector.live_worker import Worker
        async def run(root):
            p=ExtendedVenue(identity(),2);h=pair()[1]
            with Worker(root,p,h) as w:
                w.add(intent());w.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
                await w.tick('bounded-test')
                p.fraction=D('.5');await w.tick('bounded-test');self.assertEqual(h.calls,[])
                p.fraction=D(1);p.result='filled';await w.tick('bounded-test')
                self.assertEqual(len(h.calls),1)
                self.assertEqual(h.calls[0].quantity,D('.02'))
                h.fraction=D(1);h.result='filled';await w.tick('bounded-test')
                s=w.state('bounded-test');self.assertEqual(s['status'],'COMPLETED_CONFIRMED')
                self.assertEqual(D(s['fees_usdc']),D('.02'))
                self.assertEqual({k:D(v) for k,v in s['fees_by_currency'].items()},{'USD':D('.02'),'USDC':D('.02')})
                from funding_collector.supervised import status
                observed=status(Path(root).parent)['tasks'][0]
                self.assertEqual(observed['fees_by_currency'],s['fees_by_currency'])
        with tempfile.TemporaryDirectory() as root:asyncio.run(run(str(Path(root)/'worker')))
