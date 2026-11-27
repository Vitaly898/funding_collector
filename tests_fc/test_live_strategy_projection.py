"""Authoritative fill projections, no mark/P&L invented from acknowledgements."""
from decimal import Decimal
import pytest

def test_weighted_averages_leg_fees_unknown_pnl_and_sell_exposure():
    from funding_collector.live_web_service import strategy_analytics
    state=dict(intent=dict(side='sell'),fills={
        'primary':[dict(id='p1',quantity='1',price='100',fee='0.1',fee_currency='USDC'),dict(id='p2',quantity='3',price='120',fee='0.2',fee_currency='USDC')],
        'hedge':[dict(id='h1',quantity='4',price='110',fee='0.4',fee_currency='USD')]})
    row=strategy_analytics(state)
    assert row['primary']['average_price']=='115'
    assert row['primary']['signed_base']=='-4'
    assert row['hedge']['signed_base']=='4'
    assert row['primary']['fees_by_currency']=={'USDC':'0.3'}
    assert row['hedge']['fees_by_currency']=={'USD':'0.4'}
    for leg in row.values():
        assert leg['realized_pnl'] is None and leg['unrealized_pnl'] is None and leg['funding'] is None
    empty=strategy_analytics(dict(intent=dict(side='buy'),fills={'primary':[],'hedge':[]}))
    assert empty['primary']['average_price'] is None
    assert empty['primary']['fees_by_currency'] is None
    assert empty['hedge']['signed_base']=='0'

def test_hl_preserves_authoritative_fill_timestamp_when_present():
    import asyncio
    from types import SimpleNamespace
    from funding_collector.live_hyperliquid import HyperliquidPrivate
    bridge=HyperliquidPrivate.__new__(HyperliquidPrivate)
    bridge.authority=SimpleNamespace(require_read=lambda identity:None)
    bridge.identity=SimpleNamespace(account='synthetic');bridge.market=SimpleNamespace(symbol='ETH')
    bridge.info=SimpleNamespace(user_fills=lambda account:[dict(oid=7,coin='ETH',feeToken='USDC',tid=1,sz='1',px='100',fee='0.1',time=1700000000123)])
    fill=asyncio.run(bridge.fills(7))[0]
    assert fill['executed_at']=='1700000000.123'


def test_worker_persists_optional_exchange_timestamps_and_projection(tmp_path):
    import asyncio
    from funding_collector.live_worker import Worker
    from funding_collector.live_web_service import LiveService
    from tests_fc.test_live_worker import pair, intent
    p,h=pair()
    (tmp_path/'private').mkdir(mode=0o700)
    primary_fills=p.fills
    async def with_timestamp(oid):
        rows=await primary_fills(oid)
        for n,row in enumerate(rows):row['executed_at']=str(1700000000+n)
        return rows
    p.fills=with_timestamp
    async def run():
        with Worker(tmp_path/'private'/'worker',p,h) as worker:
            i=intent();worker.add(i);worker.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
            await worker.tick(i['id'])
            p.fraction=Decimal(1);p.result='filled';await worker.tick(i['id'])
            h.fraction=Decimal(1);h.result='filled';await worker.tick(i['id'])
            return worker.state(i['id'])
    state=asyncio.run(run())
    projected=LiveService._project(state)
    assert [row['executed_at'] for row in projected['fills']['primary']]==['1700000000','1700000001']
    assert all(row['executed_at'] is None for row in projected['fills']['hedge'])
    service=LiveService(tmp_path/'private')
    try:
        restored=service.status()['state']
        assert restored['fills']==projected['fills']
        assert restored['analytics']==projected['analytics']
    finally:service.close()


def test_status_lists_persisted_configuration_without_unlock_or_worker(tmp_path):
    from funding_collector.live_web_service import LiveService
    from tests_fc.test_supervised import config
    with_service=LiveService(tmp_path/'private')
    try:
        with_service.configure(config())
        rows=with_service.status()['strategies'];assert len(rows)==1
        assert rows[0]['id']==config()['intent']['id']
        assert rows[0]['status']=='CONFIGURED'
        assert rows[0]['legs'][0]['symbol']=='ETH'
        assert 'account' not in str(rows) and 'signer' not in str(rows)
        assert not (tmp_path/'private/worker').exists()
    finally:with_service.close()
    restored=LiveService(tmp_path/'private')
    try:assert restored.status()['strategies'][0]['id']==config()['intent']['id']
    finally:restored.close()
