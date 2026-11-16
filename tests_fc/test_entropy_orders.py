"""Offline SDK order contracts only. No keys, signing, or exchange transport."""
import importlib.util
import unittest
from funding_collector.domain import Reject
from test_entropy import metadata, inputs


class EntropyOrderContracts(unittest.TestCase):
    def test_protected_ioc_alo_exact_coin_and_no_hidden_builder(self):
        self.assertIsNotNone(importlib.util.find_spec('funding_collector.entropy_orders'), 'offline SDK contract missing')
        from funding_collector.entropy_orders import prepare_order
        from funding_collector.entropy import resolve_contract, EntropyInstrument
        dexs,spot=inputs()
        market=EntropyInstrument(resolve_contract(metadata(),dexs,spot,'io:NVDA'))
        cloid='0x1234567890abcdef1234567890abcdef'
        for kind,post_only,tif in [('LIMIT',True,'Alo'),('LIMIT',False,'Gtc'),('MARKET',False,'Ioc')]:
            intent=prepare_order(market,'buy','1','160.01',kind,post_only,cloid)
            self.assertEqual(intent.sdk_kwargs(),dict(name='io:NVDA',is_buy=True,sz=1.0,
                limit_px=160.01,order_type={'limit':{'tif':tif}},reduce_only=False,cloid=cloid))
            self.assertEqual(intent.asset_id,110000)
            self.assertEqual(intent.sdk_kwargs(),prepare_order(market,'buy','1','160.01',kind,post_only,cloid).sdk_kwargs())
            self.assertNotIn('builder',intent.sdk_kwargs())
            with self.assertRaisesRegex(Reject,'TRADING_DISABLED'): intent.submit(object())
        for args in [('buy','1',None,'MARKET',False,cloid),('buy','1','160.01','MARKET',True,cloid),
                     ('sell','1','160.01','LIMIT',True,'not-128-bit'),('bad','1','160.01','LIMIT',True,cloid)]:
            with self.subTest(args=args),self.assertRaises(Reject):prepare_order(market,*args)

if __name__=='__main__':unittest.main()
