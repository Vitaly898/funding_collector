"""Synthetic schema fixtures; all unit tests are offline."""
import copy
from decimal import Decimal
import tempfile
import unittest
from funding_collector.domain import Reject
from funding_collector.evedex import normalize_market, normalize_book


def market():
    return dict(id='eth-usd', name='ETHUSD', type='perpetual-futures',
        **{'from':dict(id='eth',symbol='ETH'), 'to':dict(id='usd',symbol='USD')},
        lotSize=1,multiplier=1,priceIncrement='0.01',quantityIncrement='0.01',
        minQuantity='0.01',maxQuantity='100',minVolume='5',minPrice='100',maxPrice='10000',
        trading='all',marketState='OPEN',matcherState='active',label={'orderBookIndicative':False})


class EvedexTests(unittest.TestCase):
    def test_verified_base_units_and_fail_closed_identity(self):
        m=normalize_market(market())
        self.assertEqual((m.base_asset,m.price_step,m.size_step),('ETH',Decimal('.01'),Decimal('.01')))
        m.validate('.02','1800')
        for key,value in [('multiplier',10),('lotSize',10),('type','rfq'),('name','WETHUSD'),('trading','close'),('marketState','CLOSED')]:
            raw=market();raw[key]=value
            with self.subTest(key=key),self.assertRaises(Reject):normalize_market(raw)
        with self.assertRaises(Reject):m.validate('101','1800')

    def test_registry_cli_and_ui_accept_both_roles_without_private_capabilities(self):
        from funding_collector.lighter import default_registry
        from funding_collector.cli import validate_values
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            registry=default_registry(root)
            self.assertEqual(registry.connection('evedex').deployment.id,'production-161803')
            self.assertFalse(registry.capabilities('evedex').execution)
            for primary,hedge,a,b in [('evedex','core','ETHUSD','ETH'),('core','evedex','ETH','ETHUSD')]:
                for direction in ['long','short']:
                    values=dict(strategy_mode='DN_FULL_LIMIT_MARKET_V3', primary_venue=primary,
                        hedge_venue=hedge,market=a,hedge_market=b,direction=direction,
                        execution='post-only',quantity='.02',max_notional_usd='100',max_roundtrip_cost_usd='10',
                        max_book_age_seconds='30',primary_fee_bps='1.5',hedge_fee_bps='4.5')
                    self.assertTrue(validate_values(values))
            html=(Path(__file__).resolve().parents[1]/'funding_collector/ui/index.html').read_text()
            self.assertEqual(html.count('<option value="evedex">'),2)

    def test_book_exchange_time_not_receipt_and_bounded_depth(self):
        raw={'t':1000000,'asks':[{'price':'1800.01','quantity':'2'}],'bids':[{'price':'1800','quantity':'2'}]}
        b=normalize_book(raw,1001)
        self.assertEqual(b.observed_at,1000)
        self.assertEqual(b.walk('sell','.02',1001,5)['vwap'],Decimal('1800'))
        with self.assertRaises(Reject):b.walk('buy','.02',1006,5)
        raw['t']=1010000
        with self.assertRaises(Reject):normalize_book(raw,1001)
