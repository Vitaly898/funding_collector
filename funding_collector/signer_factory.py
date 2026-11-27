"""Internal child-only SDK hydration; never a credential loader or HTTP API.

Public adapters receive paths/symbols only, never keys. Constructors receive
explicit metadata/chain/nonce policy and perform no account reads. Registration
is checked at each submit, after local authority checks but before reservation
or signature. This does not narrow protocol permissions or replace review of
an isolated account. No owner key, approvals, builders, or generic signing RPC.
"""
import time
from .domain import Reject
from .keystore import _binding
from .live import DEPLOYMENTS, Identity
from .live_hyperliquid import HyperliquidPrivate
from .live_lighter import LighterPrivate


class _CorePrivate(LighterPrivate):
    async def registration(self):
        self.authority.require_read(self.identity)
        # Official CheckClient validates registered public key for this account/index.
        # Deliberately NOT called in the constructor (it performs external I/O).
        if self.signer.check_client() is not None:
            raise Reject('Core registered account/key index mismatch; no signing')
    async def submit(self, order):
        self.authority.check(self.identity,self.market.symbol,self.market.market_id,order)
        await self.registration()
        return await super().submit(order)


class _NativePrivate(HyperliquidPrivate):
    async def registration(self):
        self.authority.require_read(self.identity)
        rows=self.info.extra_agents(self.identity.account)
        if not isinstance(rows,list):
            raise Reject('agent registration unavailable')
        matches=[r for r in rows if isinstance(r,dict)
                 and str(r.get('address','')).lower()==self.identity.signer.lower()]
        # Entropy agent names are reserved exclusively for its separate key scope.
        if (len(matches)!=1 or matches[0].get('name')=='funding-collector:entropy:io'
                or type(matches[0].get('validUntil')) is not int
                or matches[0]['validUntil']<=int(time.time()*1000)+30000):
            raise Reject('dedicated agent missing or expiring; no signing')
    async def submit(self, order):
        self.authority.check(self.identity,self.market.symbol,self.market.market_id,order)
        await self.registration()
        return await super().submit(order)


from .extended_private import ExtendedPrivate


class _ExtendedPrivate(ExtendedPrivate):
    async def registration(self):
        await self.register()

    async def submit(self, order):
        self.authority.check(self.identity,self.market.symbol,self.market.market_id,order)
        await self.registration()
        return await super().submit(order)

    async def aclose(self):
        self.close()
        await self.session.close()


def aster_snapshot(public, spec):
    """Canonical private base symbol with explicit Aster USDT wire symbol."""
    from .aster import AsterInstrument
    market,book,meta=public.snapshot(spec.symbol+'USDT')
    if not isinstance(market,AsterInstrument) or market.raw['symbol']!=spec.symbol+'USDT':
        raise Reject('Aster wire market mismatch')
    market.symbol=market.base_asset
    _scope(spec,market)
    return market,book,meta


def extended_snapshot(public, spec):
    """Exact reviewed base->wire mapping; retain the original settlement metadata."""
    from .extended import private_market
    market,book,meta=public.snapshot(spec.symbol+'-USD')
    if market.symbol!=spec.symbol+'-USD':raise Reject('Extended wire market mismatch')
    market=private_market(market.raw)
    _scope(spec,market)
    return market,book,meta


def _scope(spec, market):
    if (market.symbol,market.market_id)!=(spec.symbol,spec.asset_id):
        raise Reject('public market scope mismatch')
    base=spec.symbol.removeprefix('io:') if spec.identity.deployment=='entropy-mainnet-io' else spec.symbol
    if market.base_asset!=base or not market.units_verified or market.multiplier!=1:
        raise Reject('unverified exact base units')
    return market


def sdk_factory(spec, secret, *, public_factory=None, exchange_factory=None, info_factory=None,
                signer_factory=None, account_factory=None, http_session_factory=None, aster_transport=None):
    """Default SignerSession factory; optional constructors are offline test seams.

    Only invoke inside the signer process in production. Returned authority is
    locked; SignerSession alone arms it. Public metadata/book reads are explicit
    and credential-free. Exceptions never expose SDK messages or key material.
    """
    try:
        identity=spec.identity
        if not isinstance(identity,Identity):raise Reject('typed identity required')
        authority=spec.authority()
        if _binding(identity.deployment,identity.account,identity.api_key_index,secret)!=identity:
            raise Reject('secret identity mismatch')
        if identity.deployment=='hyperliquid-mainnet-native':
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            from hyperliquid.info import Info
            from .hyperliquid import HyperliquidPublicAdapter,HyperliquidInstrument,resolve_contract
            public=(public_factory or HyperliquidPublicAdapter)(spec.artifacts)
            spot,meta,_=public._metadata()
            market=_scope(spec,HyperliquidInstrument(resolve_contract(meta,spot,spec.symbol)))
            url=DEPLOYMENTS[identity.deployment][0]
            # perp_dexs=None is intentional: [] / [''] triggers SDK perpDexs I/O.
            info=(info_factory or Info)(url,skip_ws=True,meta=meta[0],spot_meta=spot,
                                      perp_dexs=None,timeout=15)
            exchange=(exchange_factory or Exchange)(Account.from_key(secret),url,
                meta=meta[0],spot_meta=spot,account_address=identity.account,
                vault_address=None,perp_dexs=None,timeout=15)
            if info.name_to_asset(spec.symbol)!=spec.asset_id:
                raise Reject('read SDK native asset map mismatch')
            bridge=_NativePrivate(exchange,info,identity,market,authority,spec.journal)
        elif identity.deployment=='entropy-mainnet-io':
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            from hyperliquid.info import Info
            from .entropy import EntropyPublicAdapter,EntropyInstrument,resolve_contract
            from .live_entropy import EntropyPrivate
            public=(public_factory or EntropyPublicAdapter)(spec.artifacts)
            dexs,spot,meta,_=public._metadata()
            market=_scope(spec,EntropyInstrument(resolve_contract(meta,dexs,spot,spec.symbol)))
            url=DEPLOYMENTS[identity.deployment][0]
            # SDK perp_dexs=['io'] performs unbounded constructor metadata I/O.
            # Seed empty native universe, then use official setter with verified io offset.
            empty={'universe':[]}
            info=(info_factory or Info)(url,skip_ws=True,meta=empty,spot_meta=spot,perp_dexs=None,timeout=15)
            exchange=(exchange_factory or Exchange)(Account.from_key(secret),url,
                meta=empty,spot_meta=spot,account_address=identity.account,
                vault_address=None,perp_dexs=None,timeout=15)
            offset=market.market_id-market.raw['index_in_meta']
            for sdk_info in (info,exchange.info):
                sdk_info.set_perp_meta(meta[0],offset)
            bridge=EntropyPrivate(exchange,info,identity,market,authority,spec.journal)
        elif identity.deployment in ('lighter-core-mainnet','lighter-rh-mainnet'):
            import lighter
            from .lighter import LighterPublicAdapter
            public=(public_factory or LighterPublicAdapter)('rh' if identity.deployment=='lighter-rh-mainnet' else 'core',spec.artifacts)
            market=_scope(spec,public.snapshot(spec.symbol)[0])
            signer=(signer_factory or lighter.SignerClient)(DEPLOYMENTS[identity.deployment][0],
                int(identity.account),{identity.api_key_index:secret.removeprefix('0x')},
                nonce_management_type=lighter.nonce_manager.NonceManagerType.NONE,chain_id=DEPLOYMENTS[identity.deployment][1])
            accounts=(account_factory or lighter.AccountApi)(signer.api_client)
            bridge=_CorePrivate(signer,accounts,identity,market,authority,spec.journal)
        elif identity.deployment=='aster-mainnet':
            from .aster import AsterPublicAdapter
            from .live_aster import AsterPrivate
            public=(public_factory or AsterPublicAdapter)(spec.artifacts)
            market=aster_snapshot(public,spec)[0]
            bridge=AsterPrivate(identity,secret,market,authority,spec.journal,transport=aster_transport)
        elif identity.deployment=='extended-mainnet-starknet':
            import aiohttp
            from .extended import ExtendedPublicAdapter
            public=(public_factory or ExtendedPublicAdapter)(spec.artifacts)
            market=extended_snapshot(public,spec)[0]
            # Construct only after all local/public checks; no account HTTP here.
            session=(http_session_factory or (lambda:aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),trust_env=False)))()
            bridge=_ExtendedPrivate(secret,identity,market.raw,authority,spec.journal,
                                    session=session,fee_rate=spec.fee_rate_cap)
        else:
            raise Reject('unsupported hydration')
        def snapshot():
            if identity.deployment=='aster-mainnet':return aster_snapshot(public,spec)
            if identity.deployment=='extended-mainnet-starknet':return extended_snapshot(public,spec)
            return_value=public.snapshot(spec.symbol)
            _scope(spec,return_value[0])
            return return_value
        return bridge,snapshot
    except Exception:
        raise Reject('SDK hydration refused; review deployment/account/key/market binding') from None
