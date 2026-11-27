"""Entropy contracts: synthetic inputs only, no private or live network."""
import importlib
import importlib.util
import unittest
from funding_collector.domain import Reject


class EntropyMappingTests(unittest.TestCase):
    def test_exact_dex_asset_mapping_and_collateral(self):
        self.assertIsNotNone(importlib.util.find_spec('funding_collector.entropy'), 'Entropy adapter missing')
        e = importlib.import_module('funding_collector.entropy')
        meta = [{'universe': [{'name': 'io:NVDA', 'szDecimals': 3, 'maxLeverage': 25}], 'collateralToken': 0},
                [{'markPx': '160', 'oraclePx': '160', 'funding': '0.0001'}]]
        dex = [None, {'name': 'io'}]
        spot = {'tokens': [{'index': 0, 'name': 'USDC', 'tokenId': e.USDC_TOKEN_ID, 'isCanonical': True}]}
        row = e.resolve_contract(meta, dex, spot, 'io:NVDA')
        self.assertEqual(row['asset_id'], 110000)
        self.assertEqual(row['size_step'], '0.001')
        self.assertEqual(row['collateral_asset'], 'USDC')
        self.assertEqual(row['price_unit'], 'USD per io:NVDA contract unit')
        self.assertEqual(row['underlying_id'], 'entropy:io:NVDA')
        self.assertEqual(row['funding_interval_seconds'], 3600)


class EntropyTransportTests(unittest.TestCase):
    def test_public_post_allowlist_and_no_exchange(self):
        from funding_collector import entropy as e
        self.assertTrue(hasattr(e, 'EntropyPublicClient'), 'public POST client missing')
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            client = e.EntropyPublicClient(root)
            with patch('urllib.request.build_opener') as opener:
                for body in ({'type': 'clearinghouseState', 'user': '0x00'},
                             {'type': 'metaAndAssetCtxs', 'dex': ''},
                             {'type': 'l2Book', 'coin': 'NVDA'},
                             {'type': 'l2Book', 'coin': 'xyz:NVDA'},
                             {'type': 'order'}, {'type': 'spotMeta', 'user': 'x'}):
                    with self.subTest(body=body), self.assertRaises(Reject):
                        client.info(body)
                for action in ('get', 'request', 'create_order', 'cancel_order', 'withdraw', 'sign'):
                    with self.subTest(action=action), self.assertRaises(Reject):
                        getattr(client, action)('exchange')
                opener.assert_not_called()
            import io, json
            class Response(io.BytesIO):
                status = 200
                headers = {'Age': '0'}
            response = Response(b'[]')
            with patch('urllib.request.build_opener') as opener:
                opener.return_value.open.return_value = response
                raw, at = client.info({'type': 'perpDexs'})
                req = opener.return_value.open.call_args.args[0]
                self.assertEqual(req.full_url, e.INFO_URL)
                self.assertEqual(req.method, 'POST')
                self.assertEqual(json.loads(req.data), {'type': 'perpDexs'})
                self.assertEqual(raw, [])
                self.assertGreater(at, 0)
                self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'], 15)
            from pathlib import Path
            artifacts = list(Path(root).glob('*.json'))
            self.assertEqual(len(artifacts), 1)
            self.assertEqual(json.loads(artifacts[0].read_text())['body'], '[]')


def metadata(coin='io:NVDA', **extra):
    return [{'universe': [dict(name=coin, szDecimals=3, maxLeverage=25, **extra)], 'collateralToken': 0},
            [{'markPx': '160', 'oraclePx': '160', 'funding': '0.0001'}]]


def inputs():
    from funding_collector import entropy as e
    return ([None, {'name': 'io'}], {'tokens': [{'index': 0, 'name': 'USDC',
              'tokenId': e.USDC_TOKEN_ID, 'isCanonical': True}]})


class EntropyAdapterTests(unittest.TestCase):
    def test_snapshot_exact_routing_units_and_event_freshness(self):
        from funding_collector import entropy as e
        self.assertTrue(hasattr(e, 'EntropyPublicAdapter'), 'snapshot adapter missing')
        import tempfile
        from unittest.mock import patch
        dexs, spot = inputs()
        book = {'coin': 'io:NVDA', 'time': 1000000, 'levels': [
            [{'px': '159.99', 'sz': '2', 'n': 1}], [{'px': '160.01', 'sz': '2', 'n': 1}]]}
        with tempfile.TemporaryDirectory() as root, patch.object(e.EntropyPublicClient, 'info',
                side_effect=[(dexs,1000), (spot,1000), (metadata(),1000), (book,1000)]) as info:
            adapter = e.EntropyPublicAdapter(root)
            market, depth, provenance = adapter.snapshot('io:NVDA')
            self.assertEqual([c.args[0] for c in info.call_args_list], [
                {'type':'perpDexs'}, {'type':'spotMeta'}, {'type':'metaAndAssetCtxs','dex':'io'},
                {'type':'l2Book','coin':'io:NVDA'}])
            self.assertEqual(market.market_id, 110000)
            self.assertEqual(market.base_asset, 'NVDA')
            self.assertEqual(market.raw['underlying_id'], 'entropy:io:NVDA')
            self.assertEqual(provenance['collateral_asset'], 'USDC')
            self.assertEqual(provenance['exchange_event_timestamp'], 1000000)
            self.assertEqual(str(depth.walk('buy','1',1001,5)['vwap']), '160.01')
            with self.assertRaises(Reject): depth.walk('buy','3',1001,5)
            with self.assertRaises(Reject): depth.walk('buy','1',1006,5)
            market.validate('1','160.01')
            with self.assertRaises(Reject): market.validate('1','160.011')
            with self.assertRaises(Reject): market.validate('0.0001','160')
        for coin, extras in [('io:NVDA',{'isDelisted':True})]:
            with self.subTest(coin=coin), tempfile.TemporaryDirectory() as root, patch.object(e.EntropyPublicClient,'info',
                    side_effect=[(dexs,1000),(spot,1000),(metadata(coin,**extras),1000)]) as info:
                with self.assertRaises(Reject): e.EntropyPublicAdapter(root).snapshot(coin)
                self.assertEqual(info.call_count,3)  # Reject unknown units BEFORE book fetch.


class EntropyIntegrationTests(unittest.TestCase):
    def test_registry_cli_ui_and_no_private_capability(self):
        import tempfile
        from pathlib import Path
        from funding_collector.lighter import default_registry
        from funding_collector.cli import parser
        from funding_collector.web import parameters
        with tempfile.TemporaryDirectory() as root:
            registry = default_registry(root)
            self.assertIn('entropy', [c['id'] for c in registry.describe()], 'Entropy connection missing')
            self.assertEqual(registry.connection('entropy').deployment.id, 'hyperliquid-mainnet-io')
            for name in ('account_read','execution'):
                with self.assertRaises(Reject): registry.capabilities('entropy').require(name)
            payload = dict(strategy_mode='DN_FULL_LIMIT_MARKET_V3', primary_venue='entropy',
                hedge_venue='core', direction='long', market='io:NVDA', hedge_market='NVDA', quantity='1',
                execution='post-only', primary_fee_bps='0.3', hedge_fee_bps='0', max_notional_usd='500',
                max_roundtrip_cost_usd='10', max_book_age_seconds='30', scenario='partial')
            self.assertEqual(parameters(payload)['market'], 'io:NVDA')
            args = parser().parse_args(['dn-preview','--intent-id','entropy-unit','--primary-venue','entropy',
                '--hedge-venue','core','--market','io:NVDA','--hedge-market','NVDA','--quantity','1',
                '--direction','long','--max-notional-usd','500','--max-roundtrip-cost-usd','10',
                '--primary-fee-bps','0.3','--hedge-fee-bps','0'])
            self.assertEqual(args.primary_venue, 'entropy')
            html = (Path(__file__).parents[1] / 'funding_collector/ui/index.html').read_text()
            self.assertEqual(html.count('<option value="entropy">'), 2)

    def test_no_false_pair_even_with_same_ticker(self):
        from funding_collector import entropy as e
        self.assertTrue(hasattr(e,'validate_pair'), 'strict Entropy pairing missing')
        from funding_collector.adapters import Connection, Deployment
        from funding_collector.domain import Instrument
        a = Connection('entropy',Deployment('hyperliquid-mainnet-io','entropy'),'paper','a')
        b = Connection('core',Deployment('core','lighter'),'paper','b')
        dexs, spot = inputs()
        ent = e.EntropyInstrument(e.resolve_contract(metadata(),dexs,spot,'io:NVDA'))
        other = Instrument('NVDA',110,'NVDA','USD','linear_perpetual','base','1',True,
            '0.001','0.001','0.01','10',raw={'multiplier':'1','quote_multiplier':1})
        e.validate_pair([(a,ent),(b,other)])
        for changed in ('NVDA_24_5-USD','ANTHROPIC','io:NVDA'):
            other.symbol = changed
            with self.subTest(changed=changed): e.validate_pair([(a,ent),(b,other)])  # no economic equality inference
        other.symbol = 'NVDA'
        wrong = Connection('core',Deployment('other','lighter'),'paper','b')
        with self.assertRaises(Reject): e.validate_pair([(a,ent),(wrong,other)])
        other.raw['quote_multiplier'] = '1000'
        with self.assertRaises(Reject): e.validate_pair([(a,ent),(b,other)])


class EntropyCatalogTests(unittest.TestCase):
    def test_catalog_keeps_unreviewed_and_delisted_visible_without_false_pair(self):
        from funding_collector import entropy as e
        self.assertTrue(hasattr(e.EntropyPublicAdapter,'catalog'), 'public catalog missing')
        import tempfile
        from unittest.mock import patch
        dexs, spot = inputs()
        raw = metadata()
        raw[0]['universe'] += [dict(name='io:ANTH',szDecimals=3),dict(name='io:OLD',szDecimals=2,isDelisted=True)]
        raw[1] += [{},{}]
        with tempfile.TemporaryDirectory() as root, patch.object(e.EntropyPublicClient,'info',
                side_effect=[(dexs,1000),(spot,1000),(raw,1000)]):
            data = e.EntropyPublicAdapter(root).catalog()
            self.assertEqual(data['own_dex'],'io')
            self.assertEqual(data['total'],3)
            self.assertEqual(data['active_count'],2)
            self.assertEqual(data['reviewed_unit_count'],2)
            self.assertEqual(data['markets'][1]['pairing_status'],'REVIEWED_UNITS_COUNTERVENUE_REQUIRED')
            self.assertEqual(data['markets'][2]['pairing_status'],'REFUSED_DELISTED')
            self.assertFalse(data['trading_enabled'])


if __name__ == '__main__':
    unittest.main()
