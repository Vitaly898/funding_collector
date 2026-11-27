"""Metadata-driven existing private venues; synthetic identities, denied transport."""
import time
from dataclasses import replace
from unittest.mock import Mock, patch
import socket
import pytest
from funding_collector.domain import Reject
from funding_collector.live import validate_private_pair, Order
from tests_fc.test_entropy import metadata, inputs
from tests_fc.test_live_entropy import scope


def test_entropy_new_contract_units_scope_and_real_sdk_signing(tmp_path):
    from funding_collector.entropy import resolve_contract, EntropyInstrument
    from funding_collector.signer_factory import sdk_factory
    from tests_fc.test_signer_factory import book
    spec,key=scope(tmp_path)
    dexs,spot=inputs();spot=dict(spot,universe=[])
    meta=metadata('io:OAI')
    spec=replace(spec,symbol='io:OAI',asset_id=110000)
    contract=resolve_contract(meta,dexs,spot,spec.symbol)
    assert contract['units_verified'] is True
    assert contract['underlying_id']=='entropy:io:OAI'
    assert contract['price_unit']=='USD per io:OAI contract unit'
    market=EntropyInstrument(contract)
    public=Mock();public._metadata.return_value=(dexs,spot,meta,time.time())
    public.snapshot.return_value=(market,book(),{})
    with patch.object(socket.socket,'connect',side_effect=AssertionError('NO NETWORK')):
        bridge,snapshot=sdk_factory(spec,key,public_factory=lambda _:public)
        try:
            assert bridge.exchange.info.name_to_asset(spec.symbol)==110000
            wire=[]
            bridge.exchange.post=lambda route,body: wire.append(body) or {'status':'ok','response':{'data':{'statuses':[{'resting':{'oid':7}}]}}}
            bridge.info.extra_agents=lambda owner:[dict(name='funding-collector:entropy:io',address=spec.identity.signer,validUntil=int(time.time()*1000)+60000)]
            bridge.authority.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
            import asyncio
            asyncio.run(bridge.submit(Order('0x'+'af'*16,'buy','1','160','160','LIMIT')))
            assert wire[0]['action']['orders'][0]['a']==110000
            assert snapshot()[0].symbol=='io:OAI'
        finally:bridge.close()
        with pytest.raises(Reject):sdk_factory(replace(spec,asset_id=110001),key,public_factory=lambda _:public)
    # Operator economic matching is separate from exact per-leg technical scopes.
    counter=replace(spec,identity=replace(spec.identity,deployment='hyperliquid-mainnet-native',signer='0x'+'22'*20),symbol='DOGE',asset_id=7)
    validate_private_pair(spec,counter)
    with pytest.raises(Reject):validate_private_pair(spec,spec)


def test_aster_new_symbol_metadata_local_identity_and_real_signing(tmp_path):
    import asyncio
    from funding_collector.aster import AsterInstrument, AsterPublicClient
    from funding_collector.signer_factory import sdk_factory
    from tests_fc.test_aster import metadata as aster_metadata
    from tests_fc.test_aster_integration import scope, KEY
    from tests_fc.test_live_aster import Wire
    from tests_fc.test_signer_factory import book
    raw=aster_metadata('DOGE')
    market=AsterInstrument(raw,'160')
    assert type(market.market_id) is int and market.market_id>2
    assert market.market_id==AsterInstrument(dict(raw),'160').market_id
    assert AsterPublicClient(tmp_path).valid_route('fapi/v3/depth',dict(symbol='DOGEUSDT',limit=100))
    spec=replace(scope(tmp_path),symbol='DOGE',asset_id=market.market_id)
    public=Mock();public.snapshot.side_effect=lambda symbol:(AsterInstrument(raw,'160'),book(),{})
    wire=Wire()
    wire.responses={('GET','/fapi/v3/positionSide/dual'):{'dualSidePosition':False},('GET','/fapi/v3/account'):{'canTrade':True}}
    with patch.object(socket.socket,'connect',side_effect=AssertionError('NO NETWORK')):
        bridge,snapshot=sdk_factory(spec,KEY,public_factory=lambda _:public,aster_transport=wire)
        try:
            bridge.authority.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
            asyncio.run(bridge.submit(Order('0x'+'ae'*16,'buy','.1','160','160','LIMIT')))
            assert wire.calls[-1][2]['symbol']==['DOGEUSDT']
            assert 'signature' in wire.calls[-1][2]
            assert snapshot()[0].market_id==market.market_id
        finally:bridge.close()
        # Previous three-symbol ordinal IDs cannot silently select a new contract.
        with pytest.raises(Reject):sdk_factory(replace(spec,asset_id=1),KEY,public_factory=lambda _:public)
    with pytest.raises(Reject):AsterInstrument(dict(raw,status='SETTLING'),'160')
    with pytest.raises(Reject):AsterInstrument(dict(raw,marginAsset='USDC'),'160')


@pytest.mark.parametrize('deployment,symbol',[('entropy-mainnet-io','io:OAI'),('aster-mainnet','DOGEUSDT'),('extended-mainnet-starknet','XRP-USD')])
def test_venue_catalogue_and_quote_admit_metadata_not_static_tickers(tmp_path,deployment,symbol):
    from funding_collector.live_markets import MarketDirectory
    from funding_collector.aster import AsterInstrument
    from funding_collector.extended import normalize_market
    from funding_collector.entropy import EntropyInstrument,resolve_contract
    from tests_fc.test_aster import metadata as ameta
    from tests_fc.test_extended_private_worker import sdk_market
    from tests_fc.test_live_markets import adapters,HL,book
    from types import SimpleNamespace as NS
    reads=[]
    def factory(root,dep):
        reads.append(dep);now=time.time()
        if dep==HL:return adapters(root,dep)
        if dep=='entropy-mainnet-io':
            dexs,spot=inputs();row=resolve_contract(metadata(symbol),dexs,spot,symbol)
            return NS(catalog=lambda:dict(markets=[row],observed_at_unix_s=now),snapshot=lambda s:(EntropyInstrument(row),book(now),{}))
        raw=ameta('DOGE') if dep=='aster-mainnet' else sdk_market()
        if dep=='extended-mainnet-starknet':raw.update(name='XRP-USD',assetName='XRP');raw['l2Config']['syntheticId']='0x20000000000001'
        def get(route,**params):
            assert route==('fapi/v3/exchangeInfo' if dep=='aster-mainnet' else 'info/markets')
            return (dict(symbols=[raw]) if dep=='aster-mainnet' else dict(status='OK',data=[raw])),now
        return NS(client=NS(get=get),snapshot=lambda s:((AsterInstrument(raw,'3000') if dep=='aster-mainnet' else normalize_market(raw)),book(now),{}))
    directory=MarketDirectory(tmp_path,factory=factory)
    result=directory.venue(deployment)
    assert reads==[deployment]  # Never quote all rows or read an unselected venue.
    assert result['markets'][0]['symbol']==symbol and result['markets'][0]['supported']
    quote=directory.quote(deployment,HL,primary_symbol=symbol,hedge_symbol='ETH')
    expected=symbol if deployment=='entropy-mainnet-io' else 'DOGE' if deployment=='aster-mainnet' else 'XRP'
    assert quote['legs'][0]['symbol']==expected
    if deployment in ('aster-mainnet','extended-mainnet-starknet'):
        assert type(quote['legs'][0]['asset_id']) is str  # no JS wide-integer truncation


def test_wide_metadata_ids_roundtrip_config_and_reject_noncanonical(tmp_path):
    from tests_fc.test_extended_default import spec
    from funding_collector.signer_session import SessionSpec
    s=spec(tmp_path)
    assert replace(s,asset_id='9007199254740993').asset_id==9007199254740993
    for invalid in ('01','1.0','-1',True,1.5):
        with pytest.raises(Reject):replace(s,asset_id=invalid)


def test_extended_new_symbol_real_sdk_signature_and_fresh_id_fence(tmp_path):
    import asyncio
    from funding_collector.signer_factory import sdk_factory
    from funding_collector.extended import normalize_market
    from tests_fc.test_extended_default import spec
    from tests_fc.test_extended_integration import bundle
    from tests_fc.test_extended_private_worker import sdk_market, HTTP
    from tests_fc.test_signer_factory import book
    raw=sdk_market();raw.update(name='XRP-USD',assetName='XRP');raw['l2Config']['syntheticId']='0x20000000000001'
    s=replace(spec(tmp_path),symbol='XRP',asset_id=9007199254740993)
    public=Mock();public.snapshot.side_effect=lambda symbol:(normalize_market(raw),book(),{})
    http=HTTP();http.routes.update({('GET','info/markets'):{'status':'OK','data':[raw]},
        ('GET','user/fees'):{'status':'OK','data':[dict(market='XRP-USD',makerFeeRate='0',takerFeeRate='0.0001',builderFeeRate='0')]},
        ('POST','user/order'):{'status':'OK','data':dict(id=42,externalId='0x'+'ab'*16)}})
    async def run():
        with patch.object(socket.socket,'connect',side_effect=AssertionError('NO NETWORK')):
            bridge,snapshot=sdk_factory(s,bundle(),public_factory=lambda _:public,http_session_factory=lambda:http)
            try:
                bridge.authority.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
                result=await bridge.submit(Order('0x'+'ab'*16,'buy','.02','3000','3000','LIMIT'))
                assert result['status']=='ACK_ONLY'
                payload=[call[2]['json'] for call in http.calls if call[0]=='POST'][0]
                assert payload['market']=='XRP-USD' and payload['settlement']['signature']
                assert snapshot()[0].market_id==9007199254740993
            finally:bridge.close()
            with pytest.raises(Reject):sdk_factory(replace(s,asset_id=2),bundle(),public_factory=lambda _:public,http_session_factory=lambda:http)
    asyncio.run(run())
