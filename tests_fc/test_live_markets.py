"""Offline public metadata contracts; synthetic adapter responses, no credentials."""
import time
from types import SimpleNamespace
import pytest
from funding_collector.domain import Reject, Book
from tests_fc.test_hyperliquid import native_meta, native_spot

HL='hyperliquid-mainnet-native'
CORE='lighter-core-mainnet'
RH='lighter-rh-mainnet'
ENT='entropy-mainnet-io'

def adapters(root, deployment):
    from funding_collector.hyperliquid import resolve_contract
    from funding_collector.domain import Market
    from funding_collector.hyperliquid import HyperliquidInstrument
    now=time.time()
    row=resolve_contract(native_meta(),native_spot(),'ETH')
    row['asset_id']=17  # Deliberately not the usual fixture ID.
    market=HyperliquidInstrument(row)
    light=dict(symbol='ETH',market_id=89,market_type='perp',status='active',supported_size_decimals=4,
        supported_price_decimals=1,min_base_amount='0.001',min_quote_amount='10',multiplier='1',quote_multiplier='1')
    if deployment==HL:
        return SimpleNamespace(catalog=lambda:dict(markets=[row],observed_at_unix_s=now),
            snapshot=lambda symbol:(market,book(now),{}))
    return SimpleNamespace(client=SimpleNamespace(get=lambda route: (dict(code=200,order_book_details=[light]),now)),
        snapshot=lambda symbol:(Market(light),book(now),{}))

def test_catalog_distinguishes_delisted_from_invalid_units_without_enabling_unverified(tmp_path):
    from funding_collector.live_markets import MarketDirectory
    from funding_collector.hyperliquid import resolve_contract
    def listings(root, dep):
        adapter = adapters(root, dep)
        if dep == HL:
            original = adapter.catalog
            def catalog():
                data = original()
                eth = data['markets'][0]
                data['markets'] = [eth, dict(eth, coin='DOGE', units_verified=False),
                                   dict(eth, coin='OLD', is_delisted=True)]
                return data
            adapter.catalog = catalog
        return adapter
    rows = MarketDirectory(tmp_path, factory=listings).catalog(HL, CORE)['markets'][0]
    assert [(r['symbol'], r['supported'], r['reason']) for r in rows] == [
        ('ETH', True, ''),
        ('DOGE', False, 'Не поддерживается приложением: инструмент не проверен'),
        ('OLD', False, 'Снят биржей с торгов'),
    ]
    for coin in ('BTC', 'ETH', 'SOL', 'DOGE'):
        # The source resolver, not the dropdown, owns the reviewed-unit guard.
        assert resolve_contract(native_meta(coin), native_spot(), coin)['units_verified']


def book(now):
    return Book(dict(bids=[dict(price='2999',remaining_base_amount='10')],asks=[dict(price='3001',remaining_base_amount='10')]),now)

def test_catalog_resolves_dynamic_ids_and_both_primary_directions(tmp_path):
    from funding_collector.live_markets import MarketDirectory
    d=MarketDirectory(tmp_path,factory=adapters)
    for primary,hedge in [(HL,CORE),(RH,HL)]:
        row=d.catalog(primary,hedge)
        assert len(row['pairs'])==1
        p=row['pairs'][0]
        assert p['base']=='ETH'
        assert [x['asset_id'] for x in p['legs']]==([17,89] if primary==HL else [89,17])
        assert p['legs'][0]['symbol']=='ETH'
        assert 0<=time.time()-row['observed_at']<2
    q=d.quote(HL,CORE,'ETH')
    assert d.catalog(HL,CORE)['markets'][0][0]['symbol']=='ETH'
    q=d.quote(HL,CORE,primary_symbol='ETH',hedge_symbol='ETH')
    with pytest.raises(Reject):d.quote(HL,CORE,primary_symbol='ETH',hedge_symbol='BTC')
    assert [x['reference'] for x in q['legs']]==['3000','3000']
    assert q['legs'][0]['bid']=='2999'
    q=d.quote(HL,CORE,'ETH',side='buy',offset_pct='1',protection_bps='50')
    assert q['legs'][0]['price']=='2970.0'
    assert q['legs'][1]['price']=='2985.0'
    q=d.quote(HL,CORE,'ETH',side='sell',offset_pct='1',protection_bps='50')
    assert q['legs'][0]['price']=='3030.0'
    assert q['legs'][1]['price']=='3015.0'
    for bad in ['NaN','-1','101']:
        with pytest.raises(Reject):d.quote(HL,CORE,'ETH',offset_pct=bad)

@pytest.mark.parametrize('pair',[(HL,HL),(HL,ENT),(CORE,RH),('evil',CORE)])
def test_unsupported_deployments_fail_before_transport(tmp_path,pair):
    from funding_collector.live_markets import MarketDirectory
    d=MarketDirectory(tmp_path,factory=lambda *a:pytest.fail('unexpected public read'))
    with pytest.raises(Reject):d.catalog(*pair)

def test_unknown_ambiguous_stale_and_unavailable_fail_closed(tmp_path):
    from funding_collector.live_markets import MarketDirectory
    with pytest.raises(Reject):MarketDirectory(tmp_path,factory=adapters).quote(HL,CORE,'FAKE')
    def broken(*args):raise OSError('public unavailable')
    with pytest.raises(Reject):MarketDirectory(tmp_path,factory=broken).catalog(HL,CORE)
    def stale(root,dep):
        a=adapters(root,dep)
        if dep==HL:
            original=a.catalog
            a.catalog=lambda:dict(original(),observed_at_unix_s=time.time()-61)
        return a
    with pytest.raises(Reject):MarketDirectory(tmp_path,factory=stale).catalog(HL,CORE)
    def duplicate(root,dep):
        a=adapters(root,dep)
        if dep!=HL:
            original=a.client.get
            def get(route):
                raw,at=original(route);raw['order_book_details']*=2;return raw,at
            a.client.get=get
        return a
    assert MarketDirectory(tmp_path,factory=duplicate).catalog(HL,CORE)['pairs']==[]
