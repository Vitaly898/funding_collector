"""Synthetic native namespace/transport contracts; no live/private network."""
import importlib.util
import tempfile
import unittest
from unittest.mock import patch
from funding_collector.domain import Reject


def native_meta(coin='ETH', **extra):
    return [{'universe':[dict(name=coin, szDecimals=4, maxLeverage=25, **extra)], 'collateralToken':0},
            [{'markPx':'3000','funding':'0.0001'}]]


def native_spot():
    from funding_collector.entropy import USDC_TOKEN_ID
    return {'tokens':[dict(index=0,name='USDC',tokenId=USDC_TOKEN_ID,isCanonical=True)]}


class NativeAdapterTests(unittest.TestCase):
    def test_native_snapshot_identity_precision_and_freshness(self):
        from funding_collector import hyperliquid as h
        self.assertTrue(hasattr(h,'HyperliquidPublicAdapter'), 'native adapter snapshot missing')
        book={'coin':'ETH','time':1000000,'levels':[[{'px':'2999.9','sz':'1'}],[{'px':'3000.1','sz':'1'}]]}
        with tempfile.TemporaryDirectory() as root, patch.object(h.HyperliquidPublicClient,'info',
                side_effect=[(native_spot(),1000),(native_meta(),1000),(book,1000)]) as info:
            market,depth,p=h.HyperliquidPublicAdapter(root).snapshot('ETH')
            self.assertEqual([x.args[0] for x in info.call_args_list], [
                {'type':'spotMeta'},{'type':'metaAndAssetCtxs','dex':''},{'type':'l2Book','coin':'ETH'}])
            self.assertEqual(market.market_id,0)
            self.assertEqual(market.raw['underlying_id'],'crypto:ETH')
            self.assertEqual((p['dex'],p['collateral_asset'],p['oracle_price_currency']),('','USDC','USDT'))
            self.assertIn('quanto',p['settlement_basis_warning'])
            self.assertEqual(depth.walk('buy','0.02',1001,5)['vwap'],h.Decimal('3000.1'))
            with self.assertRaises(Reject): depth.walk('buy','0.02',1006,5)
            market.validate('0.02','3000.1')
            for qty,px in [('0.00001','3000'),('0.001','3000'),('0.02','3000.11'),('0.02','3000.001')]:
                with self.subTest(qty=qty,px=px),self.assertRaises(Reject):market.validate(qty,px)
            market.validate('1','123456')  # All integer prices allowed, regardless of sig figs.
        for coin in ('io:ETH','kPEPE','WBTC'):
            with tempfile.TemporaryDirectory() as root, patch.object(h.HyperliquidPublicClient,'info') as info:
                with self.subTest(coin=coin),self.assertRaises(Reject):h.HyperliquidPublicAdapter(root).snapshot(coin)
                info.assert_not_called()

    def test_catalog_namespace_collateral_and_unreviewed_contracts(self):
        from funding_collector import hyperliquid as h
        self.assertTrue(hasattr(h,'resolve_contract'),'native metadata normalization missing')
        meta=native_meta();meta[0]['universe'] += [dict(name='kPEPE',szDecimals=0),dict(name='OLD',szDecimals=1,isDelisted=True)];meta[1] += [{},{}]
        with tempfile.TemporaryDirectory() as root,patch.object(h.HyperliquidPublicClient,'info',side_effect=[(native_spot(),1000),(meta,1000)]):
            c=h.HyperliquidPublicAdapter(root).catalog()
            self.assertEqual((c['own_dex'],c['total'],c['reviewed_unit_count']),('',3,1))
            self.assertEqual(c['markets'][1]['pairing_status'],'REFUSED_UNVERIFIED_UNITS')
        for bad in (native_meta('io:ETH'), native_meta(szDecimalsWrong=1)):
            if bad[0]['universe'][0]['name']=='ETH':bad[0]['universe'][0]['szDecimals']=True
            with self.assertRaises(Reject):h.resolve_contract(bad,native_spot(),'ETH')
        spot=native_spot();spot['tokens'][0]['tokenId']='wrong'
        with self.assertRaises(Reject):h.resolve_contract(native_meta(),spot,'ETH')


class NativeIntegrationTests(unittest.TestCase):
    def test_registry_cli_ui_separate_connection_authority_and_nonce_scope(self):
        from funding_collector.lighter import default_registry
        from funding_collector.cli import parser
        from funding_collector.web import parameters
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            r=default_registry(root)
            self.assertIn('hyperliquid',[c['id'] for c in r.describe()],'native connection missing')
            hl,ent=r.connection('hyperliquid'),r.connection('entropy')
            self.assertEqual(hl.deployment.id,'hyperliquid-mainnet-native')
            self.assertNotEqual(hl.scope,ent.scope)
            self.assertNotEqual(hl.inventory_key,ent.inventory_key)
            self.assertIsNot(r.public('hyperliquid').client,r.public('entropy').client)
            for connection in ('hyperliquid','entropy'):
                for capability in ('account_read','execution','ioc','client_order_id'):
                    with self.assertRaises(Reject):r.capabilities(connection).require(capability)
            from funding_collector.hyperliquid import connection_status
            statuses=connection_status()
            self.assertEqual([x['id'] for x in statuses],['hyperliquid','entropy'])
            self.assertNotEqual(statuses[0]['future_account_config_scope'],statuses[1]['future_account_config_scope'])
            self.assertNotEqual(statuses[0]['future_nonce_scope'],statuses[1]['future_nonce_scope'])
            self.assertTrue(all(not x['account_configured'] and not x['signer_configured'] for x in statuses))
            payload=dict(strategy_mode='DN_FULL_LIMIT_MARKET_V3',primary_venue='hyperliquid',hedge_venue='core',
                direction='long',market='ETH',hedge_market='ETH',quantity='0.02',execution='post-only',
                primary_fee_bps='0',hedge_fee_bps='0',max_notional_usd='100',max_roundtrip_cost_usd='2',
                max_book_age_seconds='30',scenario='partial')
            self.assertEqual(parameters(payload)['primary_venue'],'hyperliquid')
            payload.update(primary_venue='core',hedge_venue='hyperliquid')
            self.assertEqual(parameters(payload)['hedge_venue'],'hyperliquid')
            args=parser().parse_args(['dn-preview','--intent-id','hl-test','--primary-venue','hyperliquid',
                '--hedge-venue','core','--market','ETH','--hedge-market','ETH','--quantity','0.02',
                '--direction','long','--max-notional-usd','100','--max-roundtrip-cost-usd','2',
                '--primary-fee-bps','0','--hedge-fee-bps','0'])
            self.assertEqual(args.primary_venue,'hyperliquid')
            html=(Path(__file__).parents[1]/'funding_collector/ui/index.html').read_text()
            self.assertEqual(html.count('<option value="hyperliquid">'),2)
            self.assertIn('id="hyperliquid-status"',html)
            self.assertIn('id="entropy-status"',html)

    def test_pair_identity_no_wrappers_multipliers_or_hip3(self):
        from funding_collector import hyperliquid as h
        self.assertTrue(hasattr(h,'validate_pair'),'native strict pair validation missing')
        from funding_collector.adapters import Connection,Deployment
        from funding_collector.domain import Instrument
        native=h.HyperliquidInstrument(h.resolve_contract(native_meta(),native_spot(),'ETH'))
        a=Connection('hyperliquid',Deployment('hyperliquid-mainnet-native','hyperliquid'),'paper-hl','hl')
        b=Connection('core',Deployment('core','lighter'),'paper-core','core')
        other=Instrument('ETH',0,'ETH','USD','linear_perpetual','base','1',True,
                         '0.0001','0.01','0.0001','10',raw={'multiplier':'1','quote_multiplier':'1'})
        h.validate_pair([(a,native),(b,other)])
        for symbol in ('WBTC','kETH','io:ETH','ETH-USD'):
            other.symbol=symbol
            with self.subTest(symbol=symbol),self.assertRaises(Reject):h.validate_pair([(a,native),(b,other)])
        other.symbol='ETH';other.raw['multiplier']='1000'
        with self.assertRaises(Reject):h.validate_pair([(a,native),(b,other)])
        other.raw['multiplier']='1'
        wrong=Connection('entropy',Deployment('hyperliquid-mainnet-io','entropy'),'paper-ent','ent')
        with self.assertRaises(Reject):h.validate_pair([(a,native),(wrong,other)])
        ext=Connection('extended',Deployment('starknet-mainnet','extended'),'paper-ext','ext')
        other.symbol='ETH-USD';other.raw.update(assetName='ETH',collateralAssetName='USD',name='ETH-USD')
        h.validate_pair([(a,native),(ext,other)])
        # Existing equity-only Entropy rule remains strict, even with same native ticker.
        from funding_collector.entropy import validate_pair
        with self.assertRaises(Reject):validate_pair([(a,native),(wrong,other)])


class NativeTransportTests(unittest.TestCase):
    def test_native_allowlist_separate_from_io_and_private_off(self):
        self.assertIsNotNone(importlib.util.find_spec('funding_collector.hyperliquid'), 'native adapter missing')
        from funding_collector.hyperliquid import HyperliquidPublicClient
        import io, json
        class Response(io.BytesIO):
            status = 200
            headers = {'Age': '0'}
        with tempfile.TemporaryDirectory() as root:
            c = HyperliquidPublicClient(root)
            with patch('urllib.request.build_opener') as opener:
                for body in ({'type':'metaAndAssetCtxs','dex':'io'}, {'type':'metaAndAssetCtxs'},
                             {'type':'l2Book','coin':'io:ETH'}, {'type':'l2Book','coin':'@1'},
                             {'type':'clearinghouseState','user':'0x00'}, {'type':'order'},
                             {'type':'l2Book','coin':'ETH','user':'x'}):
                    with self.subTest(body=body), self.assertRaises(Reject): c.info(body)
                for action in ('request','get','create_order','cancel_order','withdraw','sign'):
                    with self.assertRaises(Reject): getattr(c,action)('exchange')
                opener.assert_not_called()
            with patch('urllib.request.build_opener') as opener:
                opener.return_value.open.return_value = Response(b'[]')
                self.assertEqual(c.info({'type':'metaAndAssetCtxs','dex':''})[0], [])
                req=opener.return_value.open.call_args.args[0]
                self.assertEqual(req.full_url,'https://api.hyperliquid.xyz/info')
                self.assertEqual(json.loads(req.data),{'type':'metaAndAssetCtxs','dex':''})
            from pathlib import Path
            files=list(Path(root).glob('hyperliquid-*.json'))
            self.assertEqual(len(files),1)
            envelope=json.loads(files[0].read_text())
            self.assertEqual((envelope['venue'],envelope['dex']),('hyperliquid',''))
            self.assertGreater(envelope['received_at_unix_s'],0)
