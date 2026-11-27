"""Dynamic native/Lighter contract units, not a ticker-equivalence guarantee.
Synthetic metadata/books and denied exchange transport only.
"""
from decimal import Decimal
import time
import pytest
from funding_collector.domain import Reject
from tests_fc.test_hyperliquid import native_meta, native_spot


def test_native_valid_new_symbols_use_exact_contract_identity_and_live_ordering():
    from funding_collector.hyperliquid import resolve_contract, HyperliquidInstrument, HyperliquidPublicClient
    for symbol in ('XRP', 'DOGE', 'kPEPE', 'TST'):
        meta = native_meta('ETH')
        meta[0]['universe'].append(dict(name=symbol, szDecimals=2, maxLeverage=10))
        meta[1].append(dict(markPx='2.5'))
        contract = resolve_contract(meta, native_spot(), symbol)
        assert contract['units_verified'] is True
        assert contract['underlying_id'] == 'hyperliquid-mainnet-native:' + symbol
        market = HyperliquidInstrument(contract)
        assert (market.symbol, market.market_id, market.base_asset) == (symbol, 1, symbol)
        assert market.multiplier == 1 and market.quantity_unit == 'base'
        market.validate('4', '2.5')
        with pytest.raises(Reject): market.validate('0.001', '2.5')
        assert HyperliquidPublicClient.valid_payload(None, {'type':'l2Book', 'coin':symbol})
        meta[0]['universe'].reverse(); meta[1].reverse()
        assert resolve_contract(meta, native_spot(), symbol)['asset_id'] == 0


import json
from types import SimpleNamespace
from unittest.mock import patch
from funding_collector.live_markets import MarketDirectory
from tests_fc.test_live_markets import HL, CORE
from tests_fc.test_supervised import config


def dynamic_adapters(root, deployment):
    from funding_collector.hyperliquid import HyperliquidPublicAdapter
    from funding_collector.lighter import LighterPublicAdapter
    now=time.time()
    if deployment == HL:
        adapter=HyperliquidPublicAdapter(root)
        meta=native_meta('ETH')
        meta[0]['universe'].append(dict(name='XRP',szDecimals=2,maxLeverage=10))
        meta[1].append(dict(markPx='2.5'))
        def info(payload):
            if payload['type']=='spotMeta':return dict(native_spot(),universe=[]),now
            if payload['type']=='metaAndAssetCtxs':return meta,now
            return dict(coin=payload['coin'],time=int(now*1000),levels=[[dict(px='2.49',sz='100')],[dict(px='2.51',sz='100')]]),now
        adapter.client.info=info
        return adapter
    adapter=LighterPublicAdapter('rh' if deployment=='lighter-rh-mainnet' else 'core',root)
    raw=dict(code=200,order_book_details=[dict(symbol='DOGE',market_id=77,market_type='perp',status='active',supported_size_decimals=2,supported_price_decimals=2,min_base_amount='0.01',min_quote_amount='10')])
    def get(route,**kwargs):
        if route=='orderBookDetails':return raw,now
        return dict(code=200,bids=[dict(price='2.49',remaining_base_amount='100')],asks=[dict(price='2.51',remaining_base_amount='100')]),now
    adapter.client.get=get
    return adapter


def dynamic_config(quote):
    data=config()
    for n,leg in enumerate(quote['legs']):
        data['legs'][n].update(symbol=leg['symbol'],asset_id=leg['asset_id'],max_quantity='20',max_notional='500',max_slippage_bps='100')
    data['intent'].update(quantity='10',primary_price=quote['legs'][0]['price'],hedge_price=quote['legs'][1]['price'],primary_reference='2.5',hedge_reference='2.5')
    return data


def test_dynamic_public_catalog_quote_and_supervised_scopes_preserve_exact_ids(tmp_path):
    from funding_collector.supervised import load_config, validate_public
    directory=MarketDirectory(tmp_path/'public',factory=dynamic_adapters)
    catalog=directory.catalog(HL,CORE)
    assert [(r['symbol'],r['supported']) for r in catalog['markets'][1]] == [('DOGE',True)]
    quote=directory.quote(HL,CORE,primary_symbol='XRP',hedge_symbol='DOGE')
    assert [(r['symbol'],r['asset_id']) for r in quote['legs']] == [('XRP',1),('DOGE',77)]
    path=tmp_path/'config.json';path.write_text(json.dumps(dynamic_config(quote)))
    configured=load_config(path,tmp_path/'private')
    reader=lambda spec:dynamic_adapters(tmp_path/'read',spec.identity.deployment).snapshot(spec.symbol)
    validate_public(configured,reader=reader)
    assert [s.symbol for s in configured.specs] == ['XRP','DOGE']
    # Correct ticker does not excuse an old/reordered or swapped asset ID.
    data=dynamic_config(quote);data['legs'][0]['asset_id']=0
    path.write_text(json.dumps(data))
    with pytest.raises(Reject):validate_public(load_config(path,tmp_path/'bad'),reader=reader)


@pytest.mark.parametrize('hedge_deployment',[CORE,'lighter-rh-mainnet'])
def test_dynamic_specs_hydrate_and_sign_through_real_sdks_with_denied_network(tmp_path,hedge_deployment):
    import asyncio
    import socket
    from dataclasses import replace
    from unittest.mock import Mock, AsyncMock
    from funding_collector.supervised import load_config
    from funding_collector.signer_factory import sdk_factory
    from funding_collector.signer_session import validate_snapshot
    from funding_collector.live import Order
    from tests_fc.test_supervised import KEYS
    directory=MarketDirectory(tmp_path/'public',factory=dynamic_adapters)
    quote=directory.quote(HL,hedge_deployment,primary_symbol='XRP',hedge_symbol='DOGE')
    data=dynamic_config(quote);data['legs'][1]['identity']['deployment']=hedge_deployment
    path=tmp_path/'config.json';path.write_text(json.dumps(data))
    configured=load_config(path,tmp_path/'private')
    async def run():
        for n,scope in enumerate(configured.specs):
            public=dynamic_adapters(tmp_path/str(n),scope.identity.deployment)
            bridge,snapshot=sdk_factory(scope,KEYS[n],public_factory=lambda *args:public)
            try:
                bridge.authority.arm('ARM EXACT SCOPED ORDERS',lifetime=10)
                order=Order('0x'+'ab'*16,'buy' if n==0 else 'sell','10',quote['legs'][n]['price'],'2.5','LIMIT' if n==0 else 'MARKET')
                validate_snapshot(bridge,snapshot(),order)
                if n==0:
                    bridge.info.extra_agents=Mock(return_value=[dict(address=scope.identity.signer,validUntil=int(time.time()*1000)+60000)])
                    bridge.exchange.post=Mock(return_value=dict(status='ok',response=dict(data=dict(statuses=[dict(resting=dict(oid=7))]))))
                    assert (await bridge.submit(order))['status']=='ACK_ONLY'
                    wire=bridge.exchange.post.call_args.args[1]['action']['orders'][0]
                    assert (wire['a'],wire['s'])==(1,'10')
                else:
                    bridge.signer.check_client=Mock(return_value=None)
                    bridge.signer.tx_api.next_nonce=AsyncMock(return_value=SimpleNamespace(code=200,nonce=1))
                    bridge.signer.tx_api.send_tx=AsyncMock(return_value=SimpleNamespace(code=200))
                    signed=[];original=bridge.signer.sign_create_order
                    def sign(**kwargs):
                        result=original(**kwargs);signed.append(json.loads(result[1]));return result
                    with patch.object(bridge.signer,'sign_create_order',side_effect=sign):
                        assert (await bridge.submit(order))['status']=='ACK_ONLY'
                    assert (signed[0]['MarketIndex'],signed[0]['BaseAmount'])==(77,1000)
            finally:
                bridge.close()
                if n:await bridge.signer.close()
            constructor=Mock(side_effect=AssertionError('must reject stale ID before SDK'))
            with pytest.raises(Reject):
                sdk_factory(replace(scope,asset_id=scope.asset_id+1),KEYS[n],public_factory=lambda *args:public,signer_factory=constructor,exchange_factory=constructor)
            constructor.assert_not_called()
    with patch.object(socket.socket,'connect',side_effect=AssertionError('NO NETWORK')):
        asyncio.run(run())


def test_dynamic_config_reaches_worker_exact_scoped_primary_then_hedge(tmp_path):
    import asyncio
    from funding_collector.supervised import load_config, validate_public
    from funding_collector.live_worker import Worker
    from tests_fc.test_live_worker import FakeVenue
    directory=MarketDirectory(tmp_path/'public',factory=dynamic_adapters)
    quote=directory.quote(HL,CORE,primary_symbol='XRP',hedge_symbol='DOGE')
    path=tmp_path/'config.json';path.write_text(json.dumps(dynamic_config(quote)))
    configured=load_config(path,tmp_path/'private')
    reader=lambda spec:dynamic_adapters(tmp_path/'read',spec.identity.deployment).snapshot(spec.symbol)
    validate_public(configured,reader=reader)
    class ScopedFake(FakeVenue):
        async def submit(self,order):
            self.authority.check(self.identity,self.market.symbol,self.market.market_id,order)
            self.market.validate(order.quantity,order.price)
            self.calls.append(order);self.orders[order.client_id]=order
            return dict(status='ACK_ONLY',filled_base=None)
        async def positions(self):
            if not self.calls or not self.fraction:return []
            order=self.calls[0]
            return [dict(symbol=self.market.symbol,quantity=str(order.quantity*self.fraction*(1 if order.side=='buy' else -1)))]
    venues=[]
    for spec in configured.specs:
        venue=ScopedFake(spec.identity,spec.asset_id)
        venue.market=reader(spec)[0];venue.authority=spec.authority();venues.append(venue)
    async def run():
        primary,hedge=venues
        with Worker(tmp_path/'worker',primary,hedge) as worker:
            worker.add(configured.intent)
            await worker.tick(configured.intent['id']);assert not primary.calls
            worker.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
            await worker.tick(configured.intent['id']);assert len(primary.calls)==1 and not hedge.calls
            primary.fraction=Decimal(1);primary.result='filled'
            await worker.tick(configured.intent['id']);assert len(hedge.calls)==1
            hedge.fraction=Decimal(1);hedge.result='filled'
            await worker.tick(configured.intent['id'])
            assert worker.state(configured.intent['id'])['status']=='COMPLETED_CONFIRMED'
            assert [v.market.symbol for v in venues]==['XRP','DOGE']
            assert [v.market.market_id for v in venues]==[1,77]
            assert all(v.calls[0].quantity==10 for v in venues)
    asyncio.run(run())


@pytest.mark.parametrize('change',[dict(market_id=True),dict(market_id=1.2),dict(supported_size_decimals=True),dict(supported_price_decimals=2.5),dict(multiplier='1000'),dict(status='inactive'),dict(contract_type='inverse'),dict(symbol='io:NVDA'),dict(symbol=''),dict(symbol=True)])
def test_dynamic_lighter_requires_actual_integer_metadata_and_unit_linear_perps(change):
    from funding_collector.lighter import normalize_market
    raw=dict(symbol='DOGE',market_id=77,market_type='perp',status='active',supported_size_decimals=2,supported_price_decimals=2,min_base_amount='0.01',min_quote_amount='10')
    raw.update(change)
    with pytest.raises(Reject):normalize_market(raw)


@pytest.mark.parametrize('change', [dict(isDelisted=True), dict(szDecimals=True), dict(maxLeverage=0), dict(multiplier='1000'), dict(contract_type='inverse'), dict(quantity_unit='lots'), dict(isPreIpo=True), dict(isPreIPO=True)])
def test_dynamic_native_does_not_bypass_invalid_contract_metadata(change):
    from funding_collector.hyperliquid import resolve_contract, HyperliquidInstrument
    meta = native_meta('XRP')
    meta[0]['universe'][0].update(change)
    with pytest.raises(Reject):
        HyperliquidInstrument(resolve_contract(meta, native_spot(), 'XRP'))
