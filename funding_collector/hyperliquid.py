"""Native Hyperliquid (dex=''), never Entropy/HIP-3 fallback. Public only."""
import re
from .entropy import EntropyPublicClient


class HyperliquidPublicClient(EntropyPublicClient):
    """Reuse bounded, no-redirect transport; independent exact-body policy."""
    venue = 'hyperliquid'
    dex = ''

    def valid_payload(self, payload):
        kind = payload.get('type')
        return (payload == {'type':'spotMeta'}
                or payload == {'type':'metaAndAssetCtxs', 'dex':''}
                or (kind == 'l2Book' and set(payload) == {'type','coin'}
                    and isinstance(payload['coin'], str)
                    and re.fullmatch(r'[A-Z][A-Z0-9_.-]{0,29}', payload['coin'])))


from decimal import Decimal
import time
from .domain import Instrument, Book, Reject, number
from .entropy import USDC_TOKEN_ID, INFO_URL

DOCS = 'https://hyperliquid.gitbook.io/hyperliquid-docs/'
SOURCES = [DOCS + p for p in ('trading/contract-specifications',
    'for-developers/api/asset-ids', 'for-developers/api/tick-and-lot-size',
    'for-developers/api/error-responses', 'for-developers/api/info-endpoint/perpetuals')]
# Deliberately small, explicit spot-base identities. No k/m units, wrappers, aliases.
REVIEWED_CRYPTO = {'BTC':'crypto:BTC', 'ETH':'crypto:ETH', 'SOL':'crypto:SOL'}
BASIS_WARNING = ('Official instrument type is linear perpetual, one spot-base unit. '
    'Oracle prices for reviewed BTC/ETH/SOL are USDT; collateral and PnL settle in USDC '
    'without USDT/USDC conversion (technically quanto). USD benchmark is nominal only, '
    'NOT an FX conversion or equal settlement/oracle/funding basis guarantee.')


def resolve_contract(meta_context, spot, coin):
    try:
        if not isinstance(meta_context, list) or len(meta_context) != 2:
            raise Reject('invalid native metadata/context envelope')
        meta, contexts = meta_context
        rows = meta['universe']
        names = [m['name'] for m in rows]
        if (len(rows) != len(contexts) or len(set(names)) != len(names)
                or any(not isinstance(n,str) or ':' in n or n.startswith(('@','#')) for n in names)):
            raise Reject('native metadata namespace/context identity mismatch')
        token_index = meta.get('collateralToken')
        if type(token_index) is not int or token_index != 0:
            raise Reject('native USDC collateral index mismatch')
        tokens = [t for t in spot['tokens'] if t['index'] == token_index]
        if (len(tokens) != 1 or tokens[0].get('name') != 'USDC'
                or tokens[0].get('tokenId') != USDC_TOKEN_ID or tokens[0].get('isCanonical') is not True):
            raise Reject('unverified native collateral')
        if coin not in names:
            raise Reject('exact native coin not found: ' + str(coin))
        index = names.index(coin)
        raw = rows[index]
        decimals = raw['szDecimals']
        if type(decimals) is not int or not 0 <= decimals <= 6:
            raise Reject('unsupported native size precision')
        verified = coin in REVIEWED_CRYPTO
        leverage = raw.get('maxLeverage')
        if type(leverage) is not int or leverage <= 0:
            # Unreviewed catalog entries may omit this; they are never orderable.
            if verified:
                raise Reject('missing native leverage/notional constraints')
            leverage = 1
        max_market = 30000000 if leverage >= 25 else 5000000 if leverage >= 20 else 2000000 if leverage >= 10 else 500000
        return dict(coin=coin, asset_id=index, index_in_meta=index, dex='',
            namespace='hyperliquid-mainnet-native', sz_decimals=decimals,
            size_step=format(Decimal(10) ** -decimals, 'f'),
            collateral_asset='USDC', collateral_token_id=USDC_TOKEN_ID,
            oracle_price_currency='USDT' if verified else 'UNVERIFIED',
            price_currency='USD nominal benchmark; no FX conversion',
            price_unit='USDT per spot-base unit' if verified else 'UNVERIFIED_FOR_HEDGE',
            settlement_basis_warning=BASIS_WARNING, underlying_id=REVIEWED_CRYPTO.get(coin),
            units_verified=verified, multiplier='1' if verified else None,
            is_delisted=raw.get('isDelisted',False), raw=raw, context=contexts[index],
            max_market_notional=str(max_market), max_limit_notional=str(max_market*10),
            funding_interval_seconds=3600, funding_unit='decimal fraction per hour; current estimate',
            units_sources=SOURCES)
    except (KeyError, TypeError, IndexError) as error:
        raise Reject('malformed native metadata/collateral') from error


class HyperliquidInstrument(Instrument):
    def __init__(self, contract):
        if (contract.get('dex') != '' or contract.get('namespace') != 'hyperliquid-mainnet-native'
                or not contract.get('units_verified') or contract.get('is_delisted')
                or contract.get('underlying_id') != REVIEWED_CRYPTO.get(contract.get('coin'))
                or contract.get('coin') not in REVIEWED_CRYPTO):
            raise Reject('native delisted or unverified base identity/namespace')
        self.sz_decimals = contract['sz_decimals']
        super().__init__(contract['coin'],contract['asset_id'],contract['coin'],'USD',
            'linear_perpetual','base','1',True,contract['size_step'],
            Decimal(10) ** -(6-self.sz_decimals),contract['size_step'],'10',
            contract['max_market_notional'],contract)

    def validate(self, qty, price):
        super().validate(qty,price)
        px=number(price,True).normalize()
        if px != px.to_integral_value() and len(px.as_tuple().digits)>5:
            raise Reject('Hyperliquid price exceeds 5 significant figures')


def validate_pair(entries):
    """Prove equal spot-base exposure only, NOT identical settlement/oracle basis."""
    native=[(c,m) for c,m in entries if c.deployment.venue=='hyperliquid']
    others=[(c,m) for c,m in entries if c.deployment.venue!='hyperliquid']
    if len(native)!=1 or len(others)!=1:
        raise Reject('native hedge needs one independently verified countervenue')
    hc,hm=native[0];oc,om=others[0]
    if (hc.deployment.id!='hyperliquid-mainnet-native' or not isinstance(hm,HyperliquidInstrument)
            or hm.symbol not in REVIEWED_CRYPTO or hm.raw.get('dex')!=''
            or hm.raw.get('underlying_id')!=REVIEWED_CRYPTO[hm.symbol] or hm.raw.get('is_delisted')
            or om.base_asset!=hm.base_asset or om.quote_asset!='USD'
            or om.contract_type!='linear_perpetual' or om.quantity_unit!='base'
            or om.multiplier!=1 or not om.units_verified):
        raise Reject('unproven native base identity/units/deployment')
    if oc.deployment.venue=='lighter' and oc.deployment.id in ('core','rh'):
        if (om.symbol!=hm.symbol or number(om.raw.get('multiplier','0'))!=1
                or number(om.raw.get('quote_multiplier','0'))!=1):
            raise Reject('native/Core/RH exact symbol or unit multipliers mismatch')
    elif oc.deployment.venue=='extended' and oc.deployment.id=='starknet-mainnet':
        if (om.symbol!=hm.symbol+'-USD' or om.raw.get('assetName')!=hm.symbol
                or om.raw.get('name')!=om.symbol or om.raw.get('collateralAssetName')!='USD'):
            raise Reject('native/Extended exact base and price units mismatch')
    else:
        raise Reject('unsupported native countervenue; no HIP3/Entropy/EVEDEX inference')


def connection_status():
    """Separate inert setup namespaces, not account configs or sharing authority.

    Future identical signer keys MUST NOT use independent nonce allocators:
    Hyperliquid nonce state is signer-scoped across dexes. Require an explicit
    reviewed coordination policy or dedicated agent keys before enabling either.
    No owner/key/balance is copied, inferred, loaded or configured here.
    """
    return [dict(id=name, deployment_id=deployment, dex=dex,
        public_status='adapter available; connectivity/freshness only after explicit public read',
        private_status='disabled; account and dedicated agent not configured',
        account_configured=False, signer_configured=False, trading_enabled=False,
        future_account_config_scope=name, future_agent_key_scope=name,
        future_nonce_scope=name, shared_wallet_automatic=False,
        warning='Separate explicit owner/agent setup required. Reusing the same signer across HL/Entropy shares protocol nonce state; independent nonce writers are unsafe.')
        for name,deployment,dex in [('hyperliquid','hyperliquid-mainnet-native',''),
                                   ('entropy','hyperliquid-mainnet-io','io')]]


class HyperliquidPublicAdapter:
    def __init__(self, artifacts):
        self.client=HyperliquidPublicClient(artifacts)

    def _metadata(self):
        spot,sa=self.client.info({'type':'spotMeta'})
        meta,ma=self.client.info({'type':'metaAndAssetCtxs','dex':''})
        return spot,meta,min(sa,ma)

    def catalog(self):
        spot,meta,at=self._metadata()
        markets=[resolve_contract(meta,spot,m['name']) for m in meta[0]['universe']]
        for m in markets:
            m['pairing_status']=('REFUSED_DELISTED' if m['is_delisted'] else
                'REVIEWED_BASE_COUNTERVENUE_REQUIRED' if m['units_verified'] else 'REFUSED_UNVERIFIED_UNITS')
        return dict(venue='hyperliquid',own_dex='',total=len(markets),markets=markets,
            active_count=sum(not m['is_delisted'] for m in markets),
            reviewed_unit_count=sum(m['units_verified'] and not m['is_delisted'] for m in markets),
            observed_at_unix_s=at,trading_enabled=False,settlement_basis_warning=BASIS_WARNING)

    def snapshot(self, symbol):
        if symbol not in REVIEWED_CRYPTO:
            raise Reject('native hedge supports exact reviewed BTC/ETH/SOL only; no aliases or HIP3')
        spot,meta,metadata_at=self._metadata()
        contract=resolve_contract(meta,spot,symbol)
        market=HyperliquidInstrument(contract)
        raw,receipt_at=self.client.info({'type':'l2Book','coin':symbol})
        if not isinstance(raw,dict) or raw.get('coin')!=symbol:
            raise Reject('native book coin mismatch')
        event=raw.get('time')
        if type(event) is not int or event<=0 or event/1000>receipt_at+5:
            raise Reject('native book event timestamp invalid/future')
        at=min(receipt_at,event/1000,metadata_at)
        levels=raw.get('levels')
        if not isinstance(levels,list) or len(levels)!=2:
            raise Reject('native book requires bids and asks')
        try:
            normalized={k:[dict(price=r['px'],remaining_base_amount=r['sz']) for r in rows]
                        for k,rows in zip(('bids','asks'),levels)}
            book=Book(normalized,at)
            for side in book.sides.values():
                for px,qty in side:
                    if px % market.price_step or qty % market.size_step:
                        raise Reject('native book precision mismatch')
                    if px != px.to_integral_value() and len(px.normalize().as_tuple().digits)>5:
                        raise Reject('native book significant figures mismatch')
        except (KeyError, TypeError) as error:
            raise Reject('malformed native book') from error
        return market,book,dict(base_url=INFO_URL,dex='',asset_id=market.market_id,
            namespace='hyperliquid-mainnet-native',book_observed_at_unix_s=at,
            book_age_seconds_at_preview=time.time()-at,exchange_event_timestamp=event,
            collateral_asset='USDC',collateral_token_id=USDC_TOKEN_ID,
            oracle_price_currency='USDT',settlement_basis_warning=BASIS_WARNING,
            underlying_id=contract['underlying_id'],price_unit=contract['price_unit'],multiplier='1',
            units_sources=SOURCES,context=contract['context'],funding_interval_seconds=3600,
            freshness_basis='minimum of book event, metadata and conservative HTTP Date/Age; no fallback',
            public_status='public connected / private disabled',
            market_execution='MARKET = explicit price-protected aggressive LIMIT IOC, partial/no fill possible',
            fees='explicit user assumptions; no account tier reads, builder fees or approvals')
