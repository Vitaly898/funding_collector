"""Upgrade/restart contracts; synthetic venues, no exchange transport."""
import asyncio
from decimal import Decimal as D
import tempfile
import pytest
from funding_collector.live_worker import Worker
from tests_fc.test_live_worker import pair, intent


async def resume(root, transform):
    p, h = pair()
    with Worker(root, p, h) as w:
        w.add(intent()); w.arm('ARM EXACT SCOPED ORDERS', lifetime=30)
        await w.tick('bounded-test')
        p.fraction = D('.5')
        await w.tick('bounded-test')
        saved = w.state('bounded-test')
        assert saved['primary_filled'] == '0.01'
        assert 'executed_at' not in saved['fills']['primary'][0]
        assert not h.calls
    original = p.fills
    async def upgraded(oid):
        rows = await original(oid)
        return transform(rows)
    p.fills = upgraded
    with Worker(root, p, h) as w:
        w.arm('ARM EXACT SCOPED ORDERS', lifetime=30)
        # Reobserve only the old partial: enrichment must not trigger hedge.
        partial = await w.tick('bounded-test')
        assert not h.calls
        p.fraction = D(1); p.result = 'filled'
        state = await w.tick('bounded-test')
        return p, h, w, partial, state


@pytest.mark.parametrize('timestamp', [False, True], ids=['unchanged-positive-control','upgrade-adds-timestamp'])
def test_restart_old_partial_then_new_fill_hedges_full_target_once(timestamp):
    async def run(root):
        def enrich(rows):
            if timestamp:
                for row in rows: row['executed_at'] = '1700000000'
            return rows
        p, h, w, partial, state = await resume(root, enrich)
        assert partial['status'] == 'RUNNING'
        assert state['phase'] == 'HEDGE_PENDING'
        assert state['primary_filled'] == '0.02'
        assert len(p.calls) == len(h.calls) == 1
        assert h.calls[0].quantity == D('.02')
        if timestamp: assert partial['fills']['primary'][0]['executed_at'] == '1700000000'
        # Reopen again and finish: no second primary or hedge submission.
        with Worker(root,p,h) as again:
            again.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
            h.fraction=D(1);h.result='filled'
            assert (await again.tick('bounded-test'))['status']=='COMPLETED_CONFIRMED'
            before=again.bytes('bounded-test')
            await again.tick('bounded-test')
            assert again.bytes('bounded-test')==before
            assert len(p.calls)==len(h.calls)==1
    with tempfile.TemporaryDirectory() as root: asyncio.run(run(root))


@pytest.mark.parametrize('field,value', [('quantity','0.009'),('price','3001'),('fee','0.02'),('fee_currency','USD'),('id','changed')])
def test_restart_still_rejects_changed_economic_history(field,value):
    async def run(root):
        def change(rows):
            rows[0][field]=value
            rows[0]['executed_at']='1700000000'
            return rows
        p,h,w,partial,state=await resume(root,change)
        assert partial['status']==state['status']=='ATTENTION'
        assert state['primary_filled']=='0.01'
        assert len(p.calls)==1 and not h.calls
    with tempfile.TemporaryDirectory() as root: asyncio.run(run(root))


@pytest.mark.parametrize('bad', ['NaN','Infinity','-1','0','invalid',True,{},None])
def test_malformed_execution_time_rejected_without_hedge(bad):
    async def run(root):
        def change(rows):
            rows[0]['executed_at']=bad
            return rows
        _,h,_,partial,state=await resume(root,change)
        assert partial['status']==state['status']=='ATTENTION'
        assert not h.calls
    with tempfile.TemporaryDirectory() as root: asyncio.run(run(root))


@pytest.mark.parametrize('later', [None, '1700000001'], ids=['missing-keeps-known-time','changed-time-rejected'])
def test_known_execution_time_is_never_erased_or_rewritten(later):
    async def run(root):
        p,h=pair(); original=p.fills
        async def timed(oid):
            rows=await original(oid)
            for row in rows: row['executed_at']='1700000000'
            return rows
        p.fills=timed
        with Worker(root,p,h) as w:
            w.add(intent());w.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
            await w.tick('bounded-test');p.fraction=D('.5')
            await w.tick('bounded-test')
        async def reread(oid):
            rows=await original(oid)
            if later is not None:
                for row in rows: row['executed_at']=later
            return rows
        p.fills=reread
        with Worker(root,p,h) as w:
            w.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
            state=await w.tick('bounded-test')
            assert state['status']==('RUNNING' if later is None else 'ATTENTION')
            assert state['fills']['primary'][0]['executed_at']=='1700000000'
            assert not h.calls
    with tempfile.TemporaryDirectory() as root: asyncio.run(run(root))


@pytest.mark.parametrize('bad', ['NaN','Infinity','-1','0','invalid',True,{},None])
def test_first_observation_rejects_malformed_time(bad):
    async def run(root):
        p,h=pair();original=p.fills
        async def malformed(oid):
            rows=await original(oid)
            rows[0]['executed_at']=bad
            return rows
        p.fills=malformed
        with Worker(root,p,h) as w:
            w.add(intent());w.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
            await w.tick('bounded-test');p.fraction=D(1);p.result='filled'
            state=await w.tick('bounded-test')
            assert state['status']=='ATTENTION'
            assert state['primary_filled']=='0' and not h.calls
    with tempfile.TemporaryDirectory() as root: asyncio.run(run(root))
