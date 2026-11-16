"""Credential-free Extended normalization contracts (synthetic wire fixtures)."""
import copy
import unittest
from decimal import Decimal
from funding_collector.domain import Reject

MARKET = dict(name='ETH-USD', type='PERPETUAL', assetName='ETH', collateralAssetName='USD',
              active=True, status='ACTIVE', isRfq=False, isOffHours=False, tradingHours='CONTINUOUS',
              tradingConfig=dict(minOrderSize='0.01', minOrderSizeChange='0.001', minPriceChange='0.1',
                                 maxMarketOrderValue='500000', maxLimitOrderValue='1000000'))

class ExtendedPublicTests(unittest.TestCase):
    def test_normalize_base_units_and_no_invented_notional_minimum(self):
        from funding_collector.extended import normalize_market
        market = normalize_market(MARKET)
        self.assertEqual((market.symbol, market.market_id, market.base_asset), ('ETH-USD', 'ETH-USD', 'ETH'))
        self.assertEqual((market.size_step, market.price_step, market.min_size, market.min_quote),
                         (Decimal('.001'), Decimal('.1'), Decimal('.01'), Decimal('0')))
        market.validate('.01', '2000.1')
        with self.assertRaises(Reject): market.validate('.0101', '2000.1')

    def test_snapshot_normalizes_named_book_and_checks_identity(self):
        from funding_collector.extended import ExtendedPublicAdapter
        class Client:
            def get(self, route):
                if route == 'info/markets': return {'status':'OK','data':[MARKET]}, 100
                return {'status':'OK','data':{'market':'ETH-USD','bid':[{'price':'2000','qty':'1'}], 'ask':[{'price':'2000.1','qty':'2'}]}}, 101
        adapter = ExtendedPublicAdapter.__new__(ExtendedPublicAdapter)
        adapter.client = Client()
        market, book, provenance = adapter.snapshot('ETH-USD')
        self.assertEqual(book.sides['asks'], [(Decimal('2000.1'), Decimal('2'))])
        self.assertEqual(provenance['book_observed_at_unix_s'],101)
        with self.assertRaises(Reject): adapter.snapshot('ETH')

    def test_reject_unsupported_metadata(self):
        from funding_collector.extended import normalize_market
        for key,value in [('isRfq',True),('active',False),('status','REDUCE_ONLY'),('collateralAssetName','USDC'),('isOffHours',True),('type','SPOT')]:
            with self.subTest(key=key), self.assertRaises(Reject):
                normalize_market(dict(MARKET, **{key:value}))

if __name__ == '__main__': unittest.main()

class ExtendedDNTests(unittest.TestCase):
    def test_extended_preview_reports_public_not_private_connection(self):
        from pathlib import Path
        import funding_collector
        script = (Path(funding_collector.__file__).parent / 'ui/app.js').read_text()
        self.assertIn('Public подключён · Private не настроен', script)

    def test_dn_ui_exposes_explicit_pair_and_per_leg_fees(self):
        from pathlib import Path
        import funding_collector
        page = (Path(funding_collector.__file__).parent / 'ui/index.html').read_text()
        for field in ('hedge_venue', 'hedge_market', 'primary_fee_bps', 'hedge_fee_bps'):
            self.assertIn('id="' + field + '"', page)
        self.assertIn('value="extended"', page)

    def test_explicit_dn_pair_is_selectable_without_sdk(self):
        from funding_collector.lighter import default_registry
        from funding_collector.cli import validate_values
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            registry = default_registry(root)
            self.assertEqual(registry.connection('extended').deployment.venue, 'extended')
            values = dict(strategy_mode='DN_FULL_LIMIT_MARKET_V3', primary_venue='extended', hedge_venue='core',
                          market='ETH-USD', hedge_market='ETH', direction='long', execution='post-only',
                          quantity='.02', primary_fee_bps='0', hedge_fee_bps='1',
                          max_notional_usd='100', max_roundtrip_cost_usd='2', max_book_age_seconds='30')
            self.assertTrue(validate_values(values))
            with self.assertRaises(Reject): validate_values(dict(values, hedge_venue='extended'))
