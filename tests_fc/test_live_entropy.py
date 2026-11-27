"""Bounded Entropy private contracts: genuine SDK, dummy key, no network."""
import asyncio
from dataclasses import replace
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from funding_collector.domain import Reject
from funding_collector.keystore import _binding
from funding_collector.live import Authority, Identity, Order, validate_private_pair
from funding_collector.signer_session import SessionSpec
from tests_fc.test_entropy import metadata, inputs


def scope(root):
    key='01'*32  # Public, deliberately worthless fixture, never a real account key.
    identity=_binding('entropy-mainnet-io','0x'+'11'*20,None,key)
    return SessionSpec(identity,'io:NVDA',200014,'2','500','50',
                       str(Path(root)/'signer'),str(Path(root)/'public')),key


def meta_inputs():
    dexs,spot=inputs();spot=dict(spot,universe=[])
    dexs=[None]+[{'name':'other'+str(i)} for i in range(1,10)]+[{'name':'io'}]
    meta=metadata()
    meta[0]['universe']=[dict(name='io:OTHER'+str(i),szDecimals=3) for i in range(14)]+meta[0]['universe']
    meta[1]=[{} for _ in range(14)]+meta[1]
    return dexs,spot,meta


def public_adapter():
    from funding_collector.entropy import EntropyInstrument,resolve_contract
    from funding_collector.domain import Book
    public=Mock();public._metadata.return_value=(*meta_inputs(),time.time())
    def snapshot(symbol):
        dexs,spot,meta=meta_inputs()
        market=EntropyInstrument(resolve_contract(meta,dexs,spot,symbol))
        book=Book(dict(bids=[dict(price='159.99',remaining_base_amount='5')],
                       asks=[dict(price='160.01',remaining_base_amount='5')]),time.time())
        return market,book,{}
    public.snapshot.side_effect=snapshot
    return public


def offline_session_factory(spec,key):
    """Spawn-safe real factory, genuine SDK/signing; only transport is synthetic."""
    from funding_collector.signer_factory import sdk_factory
    patch.object(socket.socket,'connect',side_effect=AssertionError('NO NETWORK')).start()
    with patch('hyperliquid.api.API.post',side_effect=AssertionError('NO CONSTRUCTOR HTTP')):
        bridge,snapshot=sdk_factory(spec,key,public_factory=lambda _:public_adapter())
    state={'wire':None,'observations':0}
    def filled():
        if not state['wire']:return '0'
        return '1' if state['observations']>=2 or state['wire']['t']['limit']['tif']=='Ioc' else '.5'
    def info(path,payload):
        kind=payload['type']
        if kind=='extraAgents':return [dict(name='funding-collector:entropy:io',address=spec.identity.signer,validUntil=int(time.time()*1000)+60000)]
        if kind=='clearinghouseState':
            wire=state['wire'];quantity=filled()
            if wire and not wire['b']:quantity='-'+quantity
            return dict(assetPositions=[] if not wire else [dict(position=dict(coin='io:NVDA',szi=quantity))])
        if kind=='openOrders':return []
        if kind=='orderStatus':
            if not state['wire']:return dict(status='unknownOid')
            state['observations']+=1
            return dict(status='order',order=dict(status='filled' if filled()=='1' else 'open',
                order=dict(coin='io:NVDA',cloid=state['wire']['c'],oid=7)))
        if kind=='userFills':
            if not state['wire']:return []
            rows=[dict(oid=7,coin='io:NVDA',feeToken='USDC',tid=8,sz='.5',px='160',fee='.01')]
            if filled()=='1':rows.append(dict(rows[0],tid=9))
            return rows
        raise AssertionError('nonallowlisted read')
    def exchange(path,payload):
        if state['wire'] is not None:raise AssertionError('duplicate submit')
        state['wire']=payload['action']['orders'][0]
        return dict(status='ok',response=dict(data=dict(statuses=[dict(resting=dict(oid=7))])))
    bridge.info.post=info;bridge.exchange.post=exchange
    return bridge,snapshot


class CounterVenue:
    """Synthetic exact Core/RH counterparty; no SDK or network claims."""
    def __init__(self,deployment):
        from types import SimpleNamespace as NS
        from decimal import Decimal
        self.identity=Identity(deployment,'123','counter',4)
        self.market=NS(symbol='NVDA',market_id=1,size_step=Decimal('.001'))
        self.authority=Authority(self.identity,'NVDA',1,'2','500','50')
        self.orders=[];self.fraction=Decimal(0)
    async def positions(self):
        if not self.orders:return []
        order=self.orders[0]
        return [dict(symbol='NVDA',quantity=str(order.quantity*self.fraction*(1 if order.side=='buy' else -1)))]
    async def open_orders(self):return []
    async def submit(self,order):
        self.authority.check(self.identity,'NVDA',1,order);self.orders.append(order)
        return dict(status='ACK_ONLY',order_id=8,filled_base=None)
    async def order_status(self,cloid):
        return dict(status='filled' if self.fraction==1 else 'open',order_id=8)
    async def fills(self,oid):
        if not self.fraction:return []
        order=self.orders[0]
        rows=[dict(id='counter-1',quantity=str(order.quantity/2),price='160',fee='.01',fee_currency='USDC')]
        if self.fraction==1:rows.append(dict(rows[0],id='counter-2'))
        return rows


class EntropyPrivateTests(unittest.TestCase):
    def _real_countervenue_factory(self,deployment):
        """Both real bridges/default dispatcher; dummy SDK keys, synthetic transport."""
        import hashlib
        import json
        import lighter
        from decimal import Decimal
        from unittest.mock import AsyncMock
        from types import SimpleNamespace as NS
        from funding_collector.signer_factory import sdk_factory
        from funding_collector.live import DEPLOYMENTS
        from funding_collector.live_entropy import EntropyPrivate
        from funding_collector.live_lighter import LighterPrivate
        from funding_collector.lighter import normalize_market
        from funding_collector.signer_session import validate_snapshot
        with tempfile.TemporaryDirectory() as root, \
             patch.object(socket.socket,'connect',side_effect=AssertionError('NO NETWORK')), \
             patch('hyperliquid.api.API.post',side_effect=AssertionError('NO CONSTRUCTOR HTTP')):
            entropy_spec,entropy_key=scope(Path(root)/'entropy')
            key='01'*40  # Deliberately public dummy Lighter key, not an account credential.
            identity=_binding(deployment,'123',4,key)
            counter_spec=SessionSpec(identity,'NVDA',1,'2','500','50',
                str(Path(root)/'counter'),str(Path(root)/'public'))
            market=normalize_market(dict(symbol='NVDA',market_id=1,market_type='perp',status='active',
                supported_size_decimals=3,supported_price_decimals=2,min_base_amount='.001',min_quote_amount='10'))
            public=Mock();public.snapshot.return_value=(market,public_adapter().snapshot('io:NVDA')[1],{})
            async def run():
                signers=[];bridges=[];constructor=lighter.SignerClient
                def signer(*args,**kwargs):
                    result=constructor(*args,**kwargs);signers.append(result);return result
                try:
                    entropy,_=sdk_factory(entropy_spec,entropy_key,public_factory=lambda _:public_adapter())
                    bridges.append(entropy)
                    with patch('funding_collector.lighter.LighterPublicAdapter',return_value=public) as adapter, \
                         patch.object(lighter,'SignerClient',side_effect=signer):
                        try:
                            counter,snapshot=sdk_factory(counter_spec,key)
                        except Reject as exc:
                            self.assertFalse(Path(counter_spec.journal).exists())
                            self.assertEqual(len(signers),1)
                            self.fail(f'reviewed {deployment} NVDA initialization refused: {exc}')
                    bridges.append(counter)
                    self.assertIsInstance(entropy,EntropyPrivate);self.assertIsInstance(counter,LighterPrivate)
                    adapter.assert_called_once_with('rh' if 'rh' in deployment else 'core',counter_spec.artifacts)
                    validate_private_pair(entropy_spec,counter_spec);validate_private_pair(counter_spec,entropy_spec)
                    self.assertNotEqual(entropy.identity.signer,counter.identity.signer)
                    self.assertEqual(counter.identity.signer,hashlib.sha256(bytes.fromhex(key)).hexdigest())
                    s=counter.signer
                    self.assertEqual((s.url,s.chain_id),DEPLOYMENTS[deployment])
                    self.assertEqual((s.account_index,s.api_key_dict),(123,{4:key}))
                    self.assertEqual(counter.authority._until,0)
                    self.assertEqual((counter.market.base_asset,counter.market.multiplier,counter.market.units_verified),
                                     ('NVDA',Decimal(1),True))
                    order=Order('0x'+'ab'*16,'sell','1','159.90','160','MARKET')
                    with self.assertRaises(Reject):await counter.submit(order)
                    s.check_client=Mock(return_value=None)
                    s.tx_api.next_nonce=AsyncMock(return_value=NS(code=200,nonce=1))
                    s.tx_api.send_tx=AsyncMock(return_value=NS(code=200))
                    counter.authority.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
                    validate_snapshot(counter,snapshot(),order)
                    with self.assertRaises(Reject):counter.authority.check(identity,'ETH',1,order)
                    with self.assertRaises(Reject):counter.authority.check(identity,'NVDA',2,order)
                    with self.assertRaises(Reject):counter.authority.check(replace(identity,account='124'),'NVDA',1,order)
                    original=s.sign_create_order
                    with patch.object(s,'sign_create_order',wraps=original) as sign:
                        result=await counter.submit(order)
                    self.assertEqual(result['status'],'ACK_ONLY');self.assertIsNone(result['filled_base'])
                    # Inspect actual SDK signing output, not a synthetic CounterVenue.
                    kw=sign.call_args.kwargs
                    self.assertEqual((kw['market_index'],kw['base_amount'],kw['price']),(1,1000,15990))
                    self.assertEqual((kw['order_type'],kw['time_in_force'],kw['api_key_index']),(1,0,4))
                    self.assertEqual(tuple(kw[k] for k in ('integrator_account_index','integrator_taker_fee','integrator_maker_fee')),(0,0,0))
                    tx=json.loads(s.tx_api.send_tx.call_args.kwargs['tx_info'])
                    self.assertEqual((tx['AccountIndex'],tx['ApiKeyIndex'],tx['MarketIndex'],tx['BaseAmount'],tx['Price']),
                                     (123,4,1,1000,15990))
                finally:
                    for bridge in bridges:bridge.close()
                    for s in signers:await s.close()
            asyncio.run(run())

    def test_default_factory_entropy_and_real_core_nvda_offline(self):
        self._real_countervenue_factory('lighter-core-mainnet')

    def test_default_factory_entropy_and_real_rh_nvda_offline(self):
        self._real_countervenue_factory('lighter-rh-mainnet')

    def _countervenue_factory_rejections(self,deployment):
        """Characterize existing fail-closed fences; no account calls or signatures."""
        from types import SimpleNamespace as NS
        from funding_collector.signer_factory import sdk_factory
        from funding_collector.lighter import normalize_market
        faults=('secret','url','chain_id','account_index','api_key_dict','key_digest',
                'AAPL','TSLA','DOGE','io:NVDA','base','asset','multiplier','quote_multiplier','quantity_unit')
        for fault in faults:
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as root, \
                 patch.object(socket.socket,'connect',side_effect=AssertionError('NO NETWORK')):
                key='01'*40;identity=_binding(deployment,'123',4,key)
                spec=SessionSpec(identity,'NVDA',1,'2','500','50',str(Path(root)/'journal'),str(Path(root)/'public'))
                raw=dict(symbol='NVDA',market_id=1,market_type='perp',status='active',supported_size_decimals=3,
                    supported_price_decimals=2,min_base_amount='.001',min_quote_amount='10')
                if fault in ('AAPL','TSLA','DOGE','io:NVDA'):
                    # These are now valid standalone symbols, but not the
                    # selected NVDA scope: metadata substitution must fail.
                    raw['symbol']=fault
                if fault=='base':raw['base_asset']='AAPL'
                if fault=='asset':raw['market_id']=2
                if fault in ('multiplier','quote_multiplier'):raw[fault]='100'
                if fault=='quantity_unit':raw['quantity_unit']='contracts'
                public=Mock();public.snapshot.side_effect=lambda symbol:(normalize_market(raw),None,{})
                def signer(url,account,keys,**kw):
                    s=NS(url=url,account_index=account,api_key_dict=keys,chain_id=kw['chain_id'],api_client=Mock())
                    bad={'url':'https://invalid.example','chain_id':0,'account_index':124,
                         'api_key_dict':{5:key},'key_digest':{4:'02'*40}}
                    if fault in bad:setattr(s,'api_key_dict' if fault=='key_digest' else fault,bad[fault])
                    return s
                with self.assertRaises(Reject):
                    sdk_factory(spec,'02'*40 if fault=='secret' else key,public_factory=lambda *a:public,
                                signer_factory=signer,account_factory=Mock())
                self.assertFalse(Path(spec.journal).exists())

    def test_core_nvda_factory_preserves_key_unit_scope_and_symbol_rejections(self):
        self._countervenue_factory_rejections('lighter-core-mainnet')

    def test_rh_nvda_factory_preserves_key_unit_scope_and_symbol_rejections(self):
        self._countervenue_factory_rejections('lighter-rh-mainnet')

    def test_real_sdk_dynamic_io_map_and_official_signed_order_offline(self):
        from funding_collector.signer_factory import sdk_factory
        from hyperliquid.api import API
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action
        with tempfile.TemporaryDirectory() as root:
            spec,key=scope(root)
            with patch.object(socket.socket,'connect',side_effect=AssertionError('NO NETWORK')), \
                 patch.object(API,'post',side_effect=AssertionError('NO CONSTRUCTOR HTTP')):
                bridge,_=sdk_factory(spec,key,public_factory=lambda _:public_adapter())
                self.addCleanup(bridge.close)
                self.assertIsInstance(bridge.exchange,Exchange)
                for info in (bridge.info,bridge.exchange.info):
                    self.assertEqual(info.name_to_asset('io:NVDA'),200014)
                    self.assertNotIn('NVDA',info.name_to_coin)
                    self.assertIsNone(info.ws_manager)
                self.assertEqual(bridge.authority._until,0)
                bridge.authority.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
                async def run():
                    for n,kind in enumerate(('LIMIT','MARKET')):
                        order=Order('0x'+str(n+1)*32,'buy','1','160','160',kind)
                        with patch.object(bridge.info,'extra_agents',return_value=[dict(
                            name='funding-collector:entropy:io',address=spec.identity.signer,
                            validUntil=int(time.time()*1000)+60000)]), \
                             patch.object(bridge.exchange,'post',return_value={'status':'ok','response':{'data':{'statuses':[{'resting':{'oid':7}}]}}}) as post:
                            self.assertEqual((await bridge.submit(order))['status'],'ACK_ONLY')
                            path,payload=post.call_args.args
                            self.assertEqual(path,'/exchange')
                            wire=payload['action']['orders'][0]
                            self.assertEqual((wire['a'],wire['s'],wire['p']),(200014,'1','160'))
                            self.assertEqual(wire['t'],{'limit':{'tif':'Alo' if kind=='LIMIT' else 'Ioc'}})
                            self.assertNotIn('builder',payload['action'])
                            self.assertIsNone(payload['vaultAddress'])
                            recovered=recover_agent_or_user_from_l1_action(payload['action'],payload['signature'],None,
                                payload['nonce'],payload['expiresAfter'],True)
                            self.assertEqual(recovered.lower(),spec.identity.signer.lower())
                asyncio.run(run())

    def test_reject_wrong_metadata_secret_and_public_market_scope_before_sdk(self):
        from funding_collector.signer_factory import sdk_factory
        for case in ('dex','collateral','asset','symbol','key'):
            with self.subTest(case=case),tempfile.TemporaryDirectory() as root:
                spec,key=scope(root);public=public_adapter();dexs,spot,meta,_=public._metadata.return_value
                if case=='dex':dexs[10]['name']='xyz'
                if case=='collateral':spot['tokens'][0]['tokenId']='0xwrong'
                if case=='asset':spec=replace(spec,asset_id=110000)
                if case=='symbol':spec=replace(spec,symbol='io:AAPL')
                if case=='key':key='02'*32
                sdk=Mock()
                with self.assertRaises(Reject):
                    sdk_factory(spec,key,public_factory=lambda _:public,exchange_factory=sdk,info_factory=sdk)
                sdk.assert_not_called()

    def test_authority_schema_pair_allowlist_and_no_hl_entropy_key_reuse(self):
        from types import SimpleNamespace as NS
        with tempfile.TemporaryDirectory() as root:
            spec,_=scope(root)
            native=Identity('hyperliquid-mainnet-native',spec.identity.account,spec.identity.signer)
            for dep,symbol in ((spec.identity,'NVDA'),(spec.identity,'io:AAPL'),(native,'@NVDA'),(native,'io:NVDA')):
                with self.subTest(symbol=symbol),self.assertRaises(Reject):Authority(dep,symbol,1,'1','500','50')
            for deployment in ('lighter-core-mainnet','lighter-rh-mainnet'):
                counter=NS(identity=Identity(deployment,'123','counter',4),symbol='NVDA')
                validate_private_pair(spec,counter);validate_private_pair(counter,spec)
                counter.symbol='ETH'
                with self.assertRaises(Reject):validate_private_pair(spec,counter)
            with self.assertRaisesRegex(Reject,'distinct dedicated'):
                validate_private_pair(spec,NS(identity=native,symbol='io:NVDA'))
            other=NS(identity=Identity('hyperliquid-mainnet-native',spec.identity.account,'0x'+'22'*20),symbol='NVDA')
            with self.assertRaises(Reject):validate_private_pair(spec,other)
            with self.assertRaises(Reject):_binding('entropy-mainnet-io',spec.identity.signer,None,'01'*32)
            with self.assertRaises(Reject):Identity('entropy-mainnet-io',spec.identity.account,spec.identity.signer,4)
            self.assertNotEqual(spec.identity.nonce_scope,native.nonce_scope)

    def test_reject_mismatched_real_sdk_read_map_account_and_agent(self):
        from funding_collector.signer_factory import sdk_factory
        from hyperliquid.info import Info
        from hyperliquid.exchange import Exchange
        class WrongMapInfo(Info):
            def set_perp_meta(self,meta,offset):
                super().set_perp_meta(meta,offset)
                if 'io:NVDA' in self.coin_to_asset:self.coin_to_asset['io:NVDA']=110000
        for fault in ('read_asset','account','agent','vault','origin'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as root, \
                 patch.object(socket.socket,'connect',side_effect=AssertionError('NO NETWORK')), \
                 patch('hyperliquid.api.API.post',side_effect=AssertionError('NO CONSTRUCTOR HTTP')):
                spec,key=scope(root)
                def exchange(*args,**kwargs):
                    ex=Exchange(*args,**kwargs)
                    if fault=='account':ex.account_address='0x'+'22'*20
                    if fault=='agent':ex.wallet=Mock(address='0x'+'22'*20)
                    if fault=='vault':ex.vault_address='0x'+'22'*20
                    if fault=='origin':ex.base_url='https://api.hyperliquid-testnet.xyz'
                    return ex
                with self.assertRaises(Reject):
                    sdk_factory(spec,key,public_factory=lambda _:public_adapter(),exchange_factory=exchange,
                                info_factory=WrongMapInfo if fault=='read_asset' else Info)

    def test_dex_reads_and_global_status_fills_exact_namespace_checks(self):
        from funding_collector.signer_factory import sdk_factory
        with tempfile.TemporaryDirectory() as root,patch.object(socket.socket,'connect',side_effect=AssertionError('NO NETWORK')):
            spec,key=scope(root);bridge,_=sdk_factory(spec,key,public_factory=lambda _:public_adapter())
            self.addCleanup(bridge.close)
            cloid='0x'+'ab'*16
            async def run():
                with patch.object(bridge.info,'post',side_effect=[{'assetPositions':[]},[],
                    {'status':'order','order':{'status':'filled','order':{'coin':'io:NVDA','cloid':cloid,'oid':7}}},
                    [dict(oid=7,coin='io:NVDA',feeToken='USDC',tid=8,sz='1',px='160',fee='.01')]]) as post:
                    self.assertEqual(await bridge.positions(),[]);self.assertEqual(await bridge.open_orders(),[])
                    self.assertEqual((await bridge.order_status(cloid))['order_id'],7)
                    self.assertEqual((await bridge.fills(7))[0]['fee_currency'],'USDC')
                    self.assertEqual([c.args[1] for c in post.call_args_list],[
                        dict(type='clearinghouseState',user=spec.identity.account,dex='io'),
                        dict(type='openOrders',user=spec.identity.account,dex='io'),
                        dict(type='orderStatus',user=spec.identity.account,oid=cloid),
                        dict(type='userFills',user=spec.identity.account)])
                invalid=[('positions',None,{'assetPositions':[{'position':{'coin':'NVDA','szi':'1'}}]}),
                         ('open_orders',None,[{'coin':'xyz:NVDA'}]),
                         ('order_status',cloid,{'status':'order','order':{'status':'filled','order':{'coin':'NVDA','cloid':cloid,'oid':7}}}),
                         ('fills',7,[dict(oid=7,coin='xyz:NVDA',feeToken='USDC',tid=8,sz='1',px='160',fee='.01')]),
                         ('fills',7,[dict(oid=7,coin='io:NVDA',feeToken='USD',tid=8,sz='1',px='160',fee='.01')])]
                for method,arg,raw in invalid:
                    with self.subTest(method=method,raw=raw),patch.object(bridge.info,'post',return_value=raw),self.assertRaises(Reject):
                        await getattr(bridge,method)(*(() if arg is None else (arg,)))
            asyncio.run(run())

    def test_registration_scope_refused_before_nonce_or_signing(self):
        from funding_collector.signer_factory import sdk_factory
        with tempfile.TemporaryDirectory() as root,patch.object(socket.socket,'connect',side_effect=AssertionError('NO NETWORK')):
            spec,key=scope(root);bridge,_=sdk_factory(spec,key,public_factory=lambda _:public_adapter())
            self.addCleanup(bridge.close);bridge.authority.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
            order=Order('0x'+'ab'*16,'buy','1','160','160','LIMIT')
            good=dict(name='funding-collector:entropy:io',address=spec.identity.signer,validUntil=int(time.time()*1000)+60000)
            for rows in ([],[dict(good,name='native')],[dict(good,address='0x'+'22'*20)],
                         [dict(good,validUntil=0)],[good,good]):
                with self.subTest(rows=rows),patch.object(bridge.info,'extra_agents',return_value=rows) as read, \
                     patch.object(bridge.exchange,'post') as post,patch.object(bridge.ledger,'reserve') as reserve, \
                     patch('hyperliquid.utils.signing.sign_l1_action') as sign:
                    with self.assertRaises(Reject):asyncio.run(bridge.submit(order))
                    read.assert_called_once_with(spec.identity.account)
                    post.assert_not_called();reserve.assert_not_called();sign.assert_not_called()

    def test_native_cannot_reuse_agent_registered_for_entropy_namespace(self):
        from funding_collector.signer_factory import _NativePrivate
        from types import SimpleNamespace as NS
        native=Identity('hyperliquid-mainnet-native','0x'+'11'*20,'0x'+'22'*20)
        bridge=object.__new__(_NativePrivate);bridge.identity=native
        bridge.authority=Authority(native,'ETH',0,'1','500','50')
        bridge.info=NS(extra_agents=Mock(return_value=[dict(address=native.signer,
            name='funding-collector:entropy:io',validUntil=int(time.time()*1000)+60000)]))
        with self.assertRaises(Reject):asyncio.run(bridge.registration())

    def test_supervised_public_nvda_config_and_inert_capabilities(self):
        import json
        from funding_collector.supervised import load_config,validate_public,status
        from funding_collector.domain import Book
        from funding_collector.entropy import EntropyInstrument,resolve_contract
        from funding_collector.lighter import normalize_market
        with tempfile.TemporaryDirectory() as root:
            spec,_=scope(root)
            legs=[dict(identity=vars(spec.identity),symbol=spec.symbol,asset_id=spec.asset_id,
                       max_quantity='2',max_notional='500',max_slippage_bps='50'),
                  dict(identity=vars(Identity('lighter-rh-mainnet','123','counter',4)),symbol='NVDA',asset_id=1,
                       max_quantity='2',max_notional='500',max_slippage_bps='50')]
            intent=dict(id='entropy-dn',quantity='1',side='buy',primary_price='160',hedge_price='159.9',primary_reference='160',hedge_reference='160')
            path=Path(root)/'config.json';path.write_text(json.dumps(dict(version=1,legs=legs,intent=intent,lifetime=30,poll_seconds=1)))
            c=load_config(path,root)
            dexs,spot,meta=meta_inputs()
            entropy=EntropyInstrument(resolve_contract(meta,dexs,spot,'io:NVDA'))
            counter=normalize_market(dict(symbol='NVDA',market_id=1,market_type='perp',status='active',supported_size_decimals=3,
                supported_price_decimals=2,min_base_amount='.001',min_quote_amount='10',multiplier='1',quote_multiplier='1'))
            book=Book(dict(bids=[dict(price='159.99',remaining_base_amount='5')],asks=[dict(price='160.01',remaining_base_amount='5')]),time.time())
            validate_public(c,reader=lambda s:(entropy if s.symbol=='io:NVDA' else counter,book,{}))
            result=status(Path(root)/'absent')
            self.assertFalse(result['trading_enabled']);self.assertFalse(result['actual_accounts_read'])
            self.assertEqual(result['private_capabilities']['entropy-mainnet-io']['reviewed_symbol'],'io:NVDA')
            self.assertFalse(result['private_capabilities']['entropy-mainnet-io']['account_acceptance_verified'])

    def test_spawned_real_sdk_session_worker_both_legs_partial_then_one_equal_ioc(self):
        from decimal import Decimal
        from funding_collector.keystore import KeyStore
        from funding_collector.signer_session import SignerSession,ACK
        from funding_collector.live_worker import Worker
        for entropy_primary in (True,False):
            with self.subTest(entropy_primary=entropy_primary),tempfile.TemporaryDirectory() as root:
                spec,key=scope(root);vault=KeyStore(Path(root)/'vault')
                vault.create(spec.identity,key,'offline dummy passphrase')
                counter=CounterVenue('lighter-core-mainnet' if entropy_primary else 'lighter-rh-mainnet')
                with SignerSession(vault,spec,factory=offline_session_factory) as session:
                    session.unlock('offline dummy passphrase',confirmation=ACK,lifetime=30)
                    primary,hedge=(session,counter) if entropy_primary else (counter,session)
                    intent=dict(id='entropy-worker',quantity='1',side='buy',primary_price='160',hedge_price='159.9',
                                primary_reference='160',hedge_reference='160')
                    async def run():
                        with Worker(Path(root)/'worker',primary,hedge) as worker:
                            worker.add(intent);worker.arm(ACK,lifetime=30)
                            await worker.tick(intent['id'])
                            if not entropy_primary:counter.fraction=Decimal('.5')
                            await worker.tick(intent['id'])
                            self.assertEqual(worker.state(intent['id'])['phase'],'PRIMARY_PENDING')
                            self.assertNotIn('hedge',worker.state(intent['id'])['orders'])
                            if not entropy_primary:counter.fraction=Decimal(1)
                            await worker.tick(intent['id'])
                            self.assertEqual(worker.state(intent['id'])['phase'],'HEDGE_PENDING')
                            if entropy_primary:counter.fraction=Decimal(1)
                            await worker.tick(intent['id'])
                            state=worker.state(intent['id'])
                            self.assertEqual(state['status'],'COMPLETED_CONFIRMED')
                            self.assertEqual(Decimal(state['residual_base']),0)
                            self.assertEqual(len(counter.orders),1)
                            self.assertEqual(counter.orders[0].quantity,Decimal(1))
                            self.assertEqual(counter.orders[0].kind,'MARKET' if entropy_primary else 'LIMIT')
                            self.assertEqual(counter.orders[0].side,'sell' if entropy_primary else 'buy')
                    asyncio.run(run())

    def test_spawned_readonly_entropy_session_cannot_submit(self):
        from funding_collector.keystore import KeyStore
        from funding_collector.signer_session import SignerSession
        with tempfile.TemporaryDirectory() as root:
            spec,key=scope(root);vault=KeyStore(Path(root)/'vault');vault.create(spec.identity,key,'offline dummy passphrase')
            with SignerSession(vault,spec,factory=offline_session_factory) as session:
                session.unlock_readonly('offline dummy passphrase',lifetime=30)
                self.assertTrue(asyncio.run(session.registration()))
                self.assertEqual(asyncio.run(session.positions()),[])
                self.assertEqual(asyncio.run(session.open_orders()),[])
                with self.assertRaises(Reject):
                    asyncio.run(session.submit(Order('0x'+'ab'*16,'buy','1','160','160','LIMIT')))
