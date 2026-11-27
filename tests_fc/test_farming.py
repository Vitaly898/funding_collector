"""Offline durable farming acceptance; no exchange transport."""
import asyncio
from decimal import Decimal as D
import pytest
from funding_collector.domain import Reject
from funding_collector.live import Order
from tests_fc.test_live_worker import pair, intent


def test_reduce_only_requires_explicit_scoped_authority():
    p, _ = pair()
    p.authority.arm('ARM EXACT SCOPED ORDERS', lifetime=30)
    order = Order('0x'+'01'*16, 'sell', D('.02'), D('3000'), D('3000'), 'MARKET', reduce_only=True)
    with pytest.raises(Reject):
        p.authority.check(p.identity, 'ETH', 1, order)
    p.authority.allow_close = True
    p.authority.check(p.identity, 'ETH', 1, order)
    with pytest.raises(Reject):
        Order('0x'+'01'*16, 'sell', D('.02'), D('3000'), D('3000'), 'MARKET', reduce_only='true')


class Venue:
    def __init__(self, source):
        self.__dict__.update(source.__dict__)
        self.authority.allow_close = True
        self.fraction = D(1)
        self.close_fraction = D(1)
        self.unknown = False
    async def positions(self):
        if self.inventory: return self.inventory
        quantity = sum((o.quantity * (self.close_fraction if o.reduce_only else self.fraction) *
                        (1 if o.side == 'buy' else -1) for o in self.calls), D(0))
        return [dict(symbol='ETH', quantity=str(quantity))] if quantity else []
    async def open_orders(self): return self.working
    async def submit(self, order):
        self.authority.check(self.identity, 'ETH', self.market.market_id, order)
        assert order.client_id not in self.orders
        self.calls.append(order); self.orders[order.client_id] = order
        return dict(status='ACK_ONLY', filled_base=None)
    async def order_status(self, client_id):
        if self.unknown or client_id not in self.orders: return dict(status='UNKNOWN')
        o = self.orders[client_id]
        fraction = self.close_fraction if o.reduce_only else self.fraction
        return dict(status='filled' if fraction == 1 else 'canceled' if o.reduce_only else 'open', order_id=client_id)
    async def fills(self, oid):
        o = self.orders[oid]; fraction = self.close_fraction if o.reduce_only else self.fraction
        return [dict(id=oid+'-fill', quantity=str(o.quantity*fraction), price=str(o.price), fee='.01', fee_currency='USDC')] if fraction else []
    async def close_quote(self, side, quantity): return dict(price='3000', reference_price='3000')


def policy(**changes):
    p=dict(hold_seconds=60, min_delay_seconds=1, max_delay_seconds=5,
           max_cycles=1, max_turnover='25000', max_fees='1')
    p.update(changes)
    return p


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_durable_one_shot_opens_holds_reduce_only_closes_both_flat(tmp_path, side):
    from funding_collector.farming import FarmingWorker
    async def run():
        p,h = [Venue(v) for v in pair()]; now=[1000]
        i=intent(); i['side']=side
        with FarmingWorker(tmp_path,p,h,clock=lambda:now[0]) as w:
            w.add_farming(i,policy()); w.arm('ARM EXACT SCOPED ORDERS',lifetime=120)
            await w.tick_farming(i['id']); await w.tick_farming(i['id'])
            assert not w.farming_state(i['id']).get('hold_deadline')
            state=await w.tick_farming(i['id'])
            assert state['phase']=='HOLDING' and state['hold_deadline']==1060
            now[0]=1059; await w.tick_farming(i['id']); assert len(p.calls)==1
            now[0]=1060
            for _ in range(5): state=await w.tick_farming(i['id'])
            assert state['phase']=='DONE'
            assert len(p.calls)==len(h.calls)==2
            assert p.calls[-1].reduce_only and h.calls[-1].reduce_only
            assert p.calls[-1].side!=p.calls[0].side and h.calls[-1].side!=h.calls[0].side
            assert not await p.positions() and not await h.positions()
            before=w.farming_bytes(i['id']); await w.tick_farming(i['id']); assert w.farming_bytes(i['id'])==before
    asyncio.run(run())


@pytest.mark.parametrize('stage',['holding','delay'])
def test_deadlines_start_after_network_reconciliation(tmp_path,stage):
    from funding_collector.farming import FarmingWorker
    async def run():
        p,h=[Venue(v) for v in pair()];now=[1000]
        with FarmingWorker(tmp_path,p,h,clock=lambda:now[0],choose_delay=lambda *_:3) as w:
            w.add_farming(intent(),policy(max_cycles=2));w.arm('ARM EXACT SCOPED ORDERS',lifetime=120)
            original=w._reconcile
            async def slow(s):
                result=await original(s)
                if (stage=='holding' and s['phase']=='OPENING') or (stage=='delay' and s['close_fills']['hedge']):
                    now[0]+=55
                return result
            w._reconcile=slow
            for _ in range(3):state=await w.tick_farming('bounded-test')
            if stage=='holding':
                assert state['hold_deadline']==1115
                now[0]=1114;await w.tick_farming('bounded-test');assert len(p.calls)==1
            else:
                now[0]=1060
                for _ in range(3):state=await w.tick_farming('bounded-test')
                assert state['phase']=='DELAY' and state['delay_deadline']==1118
                now[0]=1117;await w.tick_farming('bounded-test');assert len(p.calls)==2
    asyncio.run(run())


@pytest.mark.parametrize('field,value', [('hold_seconds',True),('hold_seconds',59),('max_cycles',0),('max_cycles',11),('max_cycles',float('inf')),('max_delay_seconds',0),('min_delay_seconds',6),('max_fees','NaN'),('max_turnover',True)])
def test_policy_rejects_invalid_bounds(field,value):
    from funding_collector.farming import validate_policy
    with pytest.raises(Reject): validate_policy(policy(**{field:value}))


def test_finite_cycles_delay_persisted_restart_never_rearms(tmp_path):
    from funding_collector.farming import FarmingWorker
    async def run():
        p,h=[Venue(v) for v in pair()];now=[1000]; chosen=[]
        def choose(lo,hi): chosen.append((lo,hi)); return 3
        with FarmingWorker(tmp_path,p,h,clock=lambda:now[0],choose_delay=choose) as w:
            w.add_farming(intent(),policy(max_cycles=2)); w.arm('ARM EXACT SCOPED ORDERS',lifetime=120)
            for _ in range(3): await w.tick_farming('bounded-test')
            now[0]=1060
            for _ in range(3): s=await w.tick_farming('bounded-test')
            assert s['phase']=='DELAY' and s['delay_deadline']==1063 and chosen==[(1,5)]
        with FarmingWorker(tmp_path,p,h,clock=lambda:now[0],choose_delay=lambda *_:pytest.fail('redraw')) as w:
            before=w.farming_bytes('bounded-test');now[0]=1064
            await w.tick_farming('bounded-test');assert w.farming_bytes('bounded-test')==before
            w.arm('ARM EXACT SCOPED ORDERS',lifetime=120)
            for _ in range(4):s=await w.tick_farming('bounded-test')
            assert s['phase']=='HOLDING' and s['cycle']==2
            now[0]=1124
            for _ in range(4):s=await w.tick_farming('bounded-test')
            assert s['phase']=='DONE' and len(p.calls)==len(h.calls)==4
            assert s['fees_by_currency']=={'USDC':'0.08'}
    asyncio.run(run())


@pytest.mark.parametrize('budget', ['max_turnover','max_fees'])
def test_budgets_stop_before_next_cycle(tmp_path,budget):
    from funding_collector.farming import FarmingWorker
    async def run():
        p,h=[Venue(v) for v in pair()];now=[1000]
        with FarmingWorker(tmp_path,p,h,clock=lambda:now[0]) as w:
            w.add_farming(intent(),policy(max_cycles=2,**{budget:'11000' if budget=='max_turnover' else '.039'}))
            w.arm('ARM EXACT SCOPED ORDERS',lifetime=120)
            for _ in range(3):await w.tick_farming('bounded-test')
            now[0]=1060
            for _ in range(8):s=await w.tick_farming('bounded-test')
            assert s['status']=='ATTENTION' and len(p.calls)==len(h.calls)==2
    asyncio.run(run())


def test_insufficient_round_trip_budget_refuses_opening_while_flat(tmp_path):
    from funding_collector.farming import FarmingWorker
    async def run():
        p,h=[Venue(v) for v in pair()]
        with FarmingWorker(tmp_path,p,h,clock=lambda:1000) as w:
            w.add_farming(intent(),policy(max_turnover='150'))
            w.arm('ARM EXACT SCOPED ORDERS',lifetime=120)
            s=await w.tick_farming('bounded-test')
            assert s['status']=='ATTENTION' and not p.calls and not h.calls
    asyncio.run(run())


def test_supervised_cli_does_not_silently_ignore_farming_policy(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from funding_collector.supervised import interactive
    import sys
    monkeypatch.setattr(sys.stdin,'isatty',lambda:True)
    monkeypatch.setattr(sys.stderr,'isatty',lambda:True)
    monkeypatch.setattr('funding_collector.supervised.open_sessions',lambda *a,**k:pytest.fail('CLI must refuse before opening signers'))
    with pytest.raises(Reject,match='browser'):
        interactive(SimpleNamespace(live_action='unlock',root=tmp_path),SimpleNamespace(farming=policy()))


def test_close_quote_is_fresh_narrow_typed_and_scoped():
    from funding_collector.signer_session import make_close_quote, SessionSpec
    from tests_fc.supervised_probe import public_reader
    from tests_fc.test_supervised import config
    from funding_collector.live import Identity
    from types import SimpleNamespace as NS
    leg=config()['legs'][0]
    spec=SessionSpec(**dict(leg,identity=Identity(**leg['identity']),journal='unused',artifacts='unused',allow_close=True))
    sample=public_reader(spec)
    bridge=NS(market=sample[0],identity=spec.identity,authority=spec.authority())
    bridge.authority.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
    quote=make_close_quote(bridge,sample,dict(side='sell',quantity='.02'))
    assert D(quote['price'])<=D(quote['reference_price'])
    with pytest.raises(Reject):make_close_quote(bridge,sample,dict(side='sell',quantity='.02',route='withdraw'))
    bridge.authority.allow_close=False
    with pytest.raises(Reject):make_close_quote(bridge,sample,dict(side='sell',quantity='.02'))


@pytest.mark.parametrize('case',['partial_primary','partial_close','asymmetric','open_orders','unknown_close','backward_clock','expired','changed_fills'])
def test_fail_closed_no_next_cycle_or_blind_close(tmp_path,case):
    from funding_collector.farming import FarmingWorker
    async def run():
        p,h=[Venue(v) for v in pair()];now=[1000]
        with FarmingWorker(tmp_path,p,h,clock=lambda:now[0]) as w:
            w.add_farming(intent(),policy(max_cycles=2));w.arm('ARM EXACT SCOPED ORDERS',lifetime=120)
            if case=='partial_primary':p.fraction=D('.5')
            for _ in range(3):s=await w.tick_farming('bounded-test')
            if case=='partial_primary':
                assert s['phase']=='OPENING' and not h.calls and 'hold_deadline' not in s
                return
            now[0]=1060
            if case=='partial_close':p.close_fraction=D('.5')
            if case=='asymmetric':h.inventory=[dict(symbol='ETH',quantity='-.019')]
            if case=='open_orders':h.working=[{}]
            if case=='backward_clock':now[0]=999
            if case=='expired':p.authority.lock()
            if case=='changed_fills':
                original=p.fills
                async def changed(oid):
                    rows=await original(oid); rows[0]['fee']='.02';return rows
                p.fills=changed
            await w.tick_farming('bounded-test')
            if case=='unknown_close':p.unknown=True
            for _ in range(5):s=await w.tick_farming('bounded-test')
            assert s['status']=='ATTENTION'
            assert len(p.calls)<=2 and len(h.calls)==1
    asyncio.run(run())
