"""Aster public contracts: synthetic exchange bodies; network forbidden."""
from decimal import Decimal as D
import socket
import tempfile
import time
import unittest
from unittest.mock import patch
from funding_collector.domain import Reject


def metadata(base='ETH'):
    return dict(symbol=base+'USDT',pair=base+'USDT',baseAsset=base,quoteAsset='USDT',marginAsset='USDT',
        contractType='PERPETUAL',status='TRADING',underlyingType='COIN',orderTypes=['LIMIT','MARKET'],
        timeInForce=['GTC','GTX','IOC'],filters=[
        dict(filterType='PRICE_FILTER',minPrice='0.01',maxPrice='1000000',tickSize='0.01'),
        dict(filterType='LOT_SIZE',minQty='0.001',maxQty='100',stepSize='0.001'),
        dict(filterType='MIN_NOTIONAL',notional='5'),
        dict(filterType='PERCENT_PRICE',multiplierUp='1.1',multiplierDown='0.9')])


class PublicTests(unittest.TestCase):
    def test_real_selection_http_validation_preview_and_repeat_safe_dn(self):
        from pathlib import Path
        from types import SimpleNamespace as NS
        from funding_collector.cli import parser, build_preview
        from funding_collector.web import parameters, App
        from funding_collector.lighter import default_registry, LighterPublicAdapter, normalize_market
        from funding_collector.aster import AsterPublicAdapter, AsterInstrument
        from funding_collector.domain import Book
        from funding_collector.original_dn import connector_status
        with tempfile.TemporaryDirectory() as root, patch.object(socket.socket,'connect',side_effect=AssertionError('network')):
            self.assertIn('aster',[r['id'] for r in default_registry(Path(root)/'p').describe()])
            self.assertTrue(next(r for r in connector_status() if r['id']=='aster')['paper_selectable'])
            html=Path('funding_collector/ui/index.html').read_text()
            self.assertEqual(html.count('<option value="aster">Aster Futures V3</option>'),2)
            for primary in ('aster','core'):
                values=dict(strategy_mode='DN_FULL_LIMIT_MARKET_V3',primary_venue=primary,
                    hedge_venue='core' if primary=='aster' else 'aster',market='ETHUSDT' if primary=='aster' else 'ETH',
                    hedge_market='ETH' if primary=='aster' else 'ETHUSDT',direction='long',quantity='0.01',execution='post-only',
                    primary_fee_bps='0',hedge_fee_bps='0',max_notional_usd='100',max_roundtrip_cost_usd='10',
                    max_book_age_seconds='30',scenario='partial')
                self.assertEqual(parameters(values),values)
                args=['dn-paper','--intent-id','aster-test']
                for k,v in values.items():
                    if k!='strategy_mode':args+=['--'+k.replace('_','-'),v]
                self.assertEqual(parser().parse_args(args).primary_venue,primary)
                def book():return Book(dict(bids=[dict(price='1999.99',remaining_base_amount='2')],asks=[dict(price='2000.01',remaining_base_amount='2')]),time.time())
                am=AsterInstrument(metadata(),'2000')
                cm=normalize_market(dict(symbol='ETH',market_id=0,market_type='perp',status='active',supported_size_decimals=3,
                    supported_price_decimals=2,min_base_amount='.001',min_quote_amount='5',multiplier='1',quote_multiplier='1'))
                with patch.object(AsterPublicAdapter,'snapshot',side_effect=lambda s:(am,book(),{'book_observed_at_unix_s':time.time()})) as a, \
                     patch.object(LighterPublicAdapter,'snapshot',side_effect=lambda s:(cm,book(),{'book_observed_at_unix_s':time.time()})):
                    app=App(Path(root)/primary)
                    draft=app.action('/api/preview',dict(intent_id='aster-test',params=values))
                    payload=dict(intent_id='aster-test',params=values,preview_id=draft['preview_id'],assume_empty_simulated=True)
                    result=app.action('/api/paper',payload)
                    self.assertEqual(result['status'],'COMPLETED_SYNTHETIC')
                    self.assertEqual(sum(e['kind']=='market_hedge_intent' for e in result['journal']),1)
                    before=Path(root,primary,'state.json').read_bytes();calls=a.call_count
                    app.action('/api/paper',payload)
                    self.assertEqual(Path(root,primary,'state.json').read_bytes(),before)
                    self.assertEqual(a.call_count,calls)

    def test_exact_metadata_executable_book_filters_and_provenance(self):
        from funding_collector.aster import AsterPublicAdapter
        with tempfile.TemporaryDirectory() as root, patch.object(socket.socket,'connect',side_effect=AssertionError('network')):
            adapter=AsterPublicAdapter(root)
            def get(route, **params):
                if route=='fapi/v3/exchangeInfo':return dict(symbols=[metadata()]),time.time()
                if route=='fapi/v3/premiumIndex':
                    self.assertEqual(params,dict(symbol='ETHUSDT'))
                    return dict(symbol='ETHUSDT',markPrice='2000'),time.time()
                self.assertEqual((route,params),('fapi/v3/depth',dict(symbol='ETHUSDT',limit=100)))
                return dict(lastUpdateId=5,E=int(time.time()*1000),T=int(time.time()*1000),bids=[['1999.99','2']],asks=[['2000.01','2']]),time.time()
            adapter.client.get=get
            market,book,source=adapter.snapshot('ETHUSDT')
            self.assertEqual((market.symbol,market.base_asset,market.quantity_unit,market.multiplier),('ETHUSDT','ETH','base',D(1)))
            self.assertEqual(book.walk('buy','0.01',time.time(),3)['notional'],D('20.0001'))
            self.assertEqual(source['price_currency'],'USDT')
            self.assertEqual(source['docs_revision'],'f3d1c073f63c90d54766e0317e10627ce8031a38')
            for qty,price in [('0.0001','2000'),('0.001','2000'),('101','2000'),('1','2000.001'),('1','2300')]:
                with self.subTest(qty=qty,price=price),self.assertRaises(Reject):market.validate(qty,price)
            market.validate('0.01','2000')
            for symbol in ['ETH','ETHUSDC','ethusdt','ETHUSDT/../order','1000ETHUSDT']:
                with self.subTest(symbol=symbol),self.assertRaises(Reject):adapter.snapshot(symbol)
            with self.assertRaises(Reject):adapter.client.get('fapi/v3/order') if False else adapter.client.request('sign')
