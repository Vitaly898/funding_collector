"""Deterministic private worker contracts. Fake exchange only, never live accounts."""
import asyncio
from decimal import Decimal as D
import tempfile
import unittest
from types import SimpleNamespace as NS
from funding_collector.domain import Reject
from funding_collector.live import Identity,Authority,Order


class FakeVenue:
    def __init__(self, identity, asset):
        self.identity=identity;self.market=NS(symbol='ETH',market_id=asset,size_step=D('.0001'))
        self.authority=Authority(identity,'ETH',asset,D('1'),D('5000'),D('50'))
        self.orders={};self.calls=[];self.inventory=[];self.working=[];self.fraction=D(0);self.result='open';self.unknown=False
    async def positions(self):
        if self.inventory:return self.inventory
        if self.calls and self.fraction:
            o=self.calls[0]
            return [dict(symbol='ETH',quantity=str(o.quantity*self.fraction*(1 if o.side=='buy' else -1)))]
        return []
    async def open_orders(self):return self.working
    async def submit(self,order):
        self.authority.check(self.identity,'ETH',self.market.market_id,order)
        self.calls.append(order);self.orders[order.client_id]=order
        return dict(status='UNKNOWN' if self.unknown else 'ACK_ONLY',filled_base=None)
    async def order_status(self,client_id):
        if client_id not in self.orders or self.unknown:return dict(status='UNKNOWN')
        return dict(status=self.result,order_id=client_id)
    async def fills(self,oid):
        o=self.orders[oid]
        if not self.fraction:return []
        if self.fraction==D('.5'):
            return [dict(id='trade-1',quantity=str(o.quantity/2),price=str(o.price),fee='0.01',fee_currency='USDC')]
        return [dict(id='trade-'+str(n),quantity=str(o.quantity/2),price=str(o.price),fee='0.01',fee_currency='USDC') for n in (1,2)]


def pair():
    return (FakeVenue(Identity('hyperliquid-mainnet-native','0x'+'11'*20,'0x'+'22'*20),1),
            FakeVenue(Identity('lighter-core-mainnet','123','dummy',4),0))


def intent():
    return dict(id='bounded-test',quantity='0.02',side='buy',primary_price='3000',hedge_price='2990',
                primary_reference='3000',hedge_reference='3000')


class WorkerTests(unittest.TestCase):
    def run_async(self,coro):return asyncio.run(coro)

    def test_disabled_then_full_primary_one_exact_hedge_fills_fees_idempotent(self):
        from funding_collector.live_worker import Worker
        async def run(root):
            p,h=pair()
            with Worker(root,p,h) as w:
                w.add(intent());await w.tick('bounded-test');self.assertEqual(p.calls,[])
                w.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
                await w.tick('bounded-test');self.assertEqual(len(p.calls),1)
                p.fraction=D('.5');await w.tick('bounded-test');self.assertEqual(h.calls,[])
                p.fraction=D(1);p.result='filled';await w.tick('bounded-test')
                self.assertEqual(len(h.calls),1);self.assertEqual(h.calls[0].quantity,D('.02'));self.assertEqual(h.calls[0].side,'sell')
                h.fraction=D(1);h.result='filled';await w.tick('bounded-test')
                state=w.state('bounded-test');self.assertEqual(state['status'],'COMPLETED_CONFIRMED')
                self.assertEqual(state['residual_base'],'0.00');self.assertEqual(state['fees_usdc'],'0.04')
                before=w.bytes('bounded-test');await w.tick('bounded-test');self.assertEqual(w.bytes('bounded-test'),before)
                self.assertEqual(len(h.calls),1)
        with tempfile.TemporaryDirectory() as root:self.run_async(run(root))

    def test_short_primary_residual_is_signed_before_and_after_partial_hedge(self):
        from funding_collector.live_worker import Worker
        async def run(root):
            p,h=pair();i=intent();i['side']='sell'
            with Worker(root,p,h) as w:
                w.add(i);w.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
                await w.tick(i['id']);p.fraction=D('.5')
                await w.tick(i['id'])
                self.assertEqual(D(w.state(i['id'])['residual_base']),D('-.01'))
                p.fraction=D(1);p.result='filled';await w.tick(i['id'])
                self.assertEqual(h.calls[0].side,'buy')
                h.fraction=D('.5');h.result='canceled';await w.tick(i['id'])
                self.assertEqual(D(w.state(i['id'])['residual_base']),D('-.01'))
        with tempfile.TemporaryDirectory() as root:self.run_async(run(root))

    def test_baseline_unknown_partial_and_rejected_fail_visible_no_resubmit(self):
        from funding_collector.live_worker import Worker
        async def run(root,scenario):
            p,h=pair()
            if scenario=='baseline':p.inventory=[{'quantity':'.01'}]
            with Worker(root,p,h) as w:
                w.add(intent());w.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
                await w.tick('bounded-test')
                if scenario=='baseline':
                    self.assertEqual(w.state('bounded-test')['status'],'ATTENTION');self.assertEqual(p.calls,[]);return
                p.fraction=D(1);p.result='filled'
                if scenario=='unknown':h.unknown=True
                await w.tick('bounded-test')
                h.fraction=D('.5') if scenario=='partial' else D(0)
                h.result='canceled' if scenario=='partial' else 'rejected'
                await w.tick('bounded-test');await w.tick('bounded-test')
                self.assertEqual(len(h.calls),1)
                self.assertEqual(w.state('bounded-test')['status'],'UNKNOWN' if scenario=='unknown' else 'ATTENTION')
                if scenario=='partial':self.assertEqual(D(w.state('bounded-test')['residual_base']),D('.010'))
        for scenario in ('baseline','partial','rejected','unknown'):
            with self.subTest(scenario=scenario),tempfile.TemporaryDirectory() as root:self.run_async(run(root,scenario))

    def test_restart_write_ahead_blocks_blind_primary_and_hedge_submission(self):
        from funding_collector.live_worker import Worker
        async def run(root,crash):
            p,h=pair()
            def hook(stage):
                if stage==crash:raise SystemExit('simulated process exit')
            with Worker(root,p,h,checkpoint=hook) as w:
                w.add(intent());w.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
                if crash=='PRIMARY_PENDING':
                    with self.assertRaises(SystemExit):await w.tick('bounded-test')
                else:
                    await w.tick('bounded-test');p.fraction=D(1);p.result='filled'
                    with self.assertRaises(SystemExit):await w.tick('bounded-test')
            with Worker(root,p,h) as w:
                w.arm('ARM EXACT SCOPED ORDERS',lifetime=30);await w.tick('bounded-test')
                self.assertEqual(w.state('bounded-test')['status'],'UNKNOWN')
                self.assertEqual(h.calls,[])
                self.assertEqual(len(p.calls),0 if crash=='PRIMARY_PENDING' else 1)
        for crash in ('PRIMARY_PENDING','HEDGE_PENDING'):
            with self.subTest(crash=crash),tempfile.TemporaryDirectory() as root:self.run_async(run(root,crash))
