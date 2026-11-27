"""Pinned official Lighter Core SDK 1.1.8 private bridge, no RH inference.

Use sign_create_order + tx_api.send_tx to avoid SDK create_order debug logging of
signed payloads. Explicit nonce, API key, chain, zero attribution and base units.
The native signer library is process-global: one dedicated workload per process.
"""
import asyncio
import hashlib
from decimal import Decimal
from .domain import Reject,number
from .live import DEPLOYMENTS,OrderLedger


class LighterPrivate:
    def __init__(self, signer, account_api, identity, market, authority, journal):
        authority.require_read(identity)
        if (identity.deployment not in ('lighter-core-mainnet','lighter-rh-mainnet') or signer.url!=DEPLOYMENTS[identity.deployment][0]
                or signer.chain_id!=DEPLOYMENTS[identity.deployment][1] or signer.account_index!=int(identity.account)
                or list(signer.api_key_dict)!=[identity.api_key_index]
                or hashlib.sha256(bytes.fromhex(signer.api_key_dict[identity.api_key_index])).hexdigest()!=identity.signer
                or (market.symbol,market.market_id)!=(authority.symbol,authority.asset_id)
                or market.multiplier!=1 or not market.units_verified or market.quantity_unit!='base'
                or market.contract_type!='linear_perpetual' or market.raw.get('market_type')!='perp'):
            raise Reject('Core SDK deployment/account/API-key/market mismatch')
        self.signer,self.accounts,self.identity,self.market,self.authority=signer,account_api,identity,market,authority
        self.ledger=OrderLedger(journal,identity)
        self.ledger.db.execute('CREATE TABLE IF NOT EXISTS client_map (n INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL)')
        self.ledger.db.commit()

    async def _call(self, awaitable):
        return await asyncio.wait_for(awaitable,timeout=15)

    def _auth(self):
        self.authority.require_read(self.identity)
        token,error=self.signer.create_auth_token_with_expiry(deadline=60,api_key_index=self.identity.api_key_index)
        if error or not token:raise Reject('Core auth unavailable')
        return token

    def _mapped(self, client_id, *, create=False):
        db=self.ledger.db
        if create:
            db.execute('INSERT OR IGNORE INTO client_map(id) VALUES (?)',(client_id,));db.commit()
        row=db.execute('SELECT n FROM client_map WHERE id=?',(client_id,)).fetchone()
        if not row or not 1 <= row[0] < 2**48:raise Reject('unmapped/out-of-range Core client id')
        return row[0]

    @staticmethod
    def _ok(response):
        if response.code!=200:raise Reject('Core response not successful; no inference of empty data')
        return response

    async def positions(self):
        self.authority.require_read(self.identity)
        raw=self._ok(await self._call(self.accounts.account(by='index',value=self.identity.account)))
        if len(raw.accounts)!=1 or raw.accounts[0].account_index!=int(self.identity.account):
            raise Reject('account positions identity mismatch')
        return [dict(symbol=p.symbol,quantity=str(number(p.position)*p.sign)) for p in raw.accounts[0].positions]

    async def open_orders(self):
        raw=self._ok(await self._call(self.signer.order_api.account_active_orders(
            authorization=self._auth(),account_index=int(self.identity.account))))
        if any(o.owner_account_index!=int(self.identity.account) for o in raw.orders):
            raise Reject('working order account mismatch')
        return [o.model_dump() for o in raw.orders]

    async def _pages(self, method, field, **kwargs):
        cursor=None;rows=[];seen=set()
        for _ in range(100):
            raw=self._ok(await self._call(method(authorization=self._auth(),account_index=int(self.identity.account),
                            limit=100,cursor=cursor,**kwargs)))
            rows.extend(getattr(raw,field));cursor=raw.next_cursor
            if not cursor:return rows
            if cursor in seen:raise Reject('repeating history cursor; reconciliation incomplete')
            seen.add(cursor)
        raise Reject('history page bound exceeded; no complete fill claim')

    async def order_status(self, client_id):
        n=self._mapped(client_id)
        active=self._ok(await self._call(self.signer.order_api.account_active_orders(
            authorization=self._auth(),account_index=int(self.identity.account),market_id=self.market.market_id)))
        rows=list(active.orders)+await self._pages(self.signer.order_api.account_inactive_orders,'orders',market_id=self.market.market_id)
        found={o.order_index:o for o in rows if o.client_order_index==n}
        if not found:return dict(status='UNKNOWN',filled_base=None)
        if len(found)!=1:raise Reject('ambiguous Core client order index')
        o=next(iter(found.values()))
        if o.owner_account_index!=int(self.identity.account) or o.market_index!=self.market.market_id:
            raise Reject('Core order account/market mismatch')
        return dict(status=o.status,order_id=o.order_index,filled_base=str(number(o.filled_base_amount)))

    async def fills(self, order_id):
        rows=await self._pages(self.signer.order_api.trades,'trades',sort_by='timestamp',sort_dir='asc',
                              market_id=self.market.market_id,order_index=order_id,aggregate=False)
        out=[];account=int(self.identity.account)
        for t in rows:
            ask=t.ask_account_id==account and t.ask_id==order_id
            bid=t.bid_account_id==account and t.bid_id==order_id
            if t.market_id!=self.market.market_id or ask==bid:raise Reject('Core fill market/account/order mismatch')
            maker=t.is_maker_ask==ask
            rate=t.maker_fee if maker else t.taker_fee
            integrator=t.integrator_maker_fee if maker else t.integrator_taker_fee
            if rate is None or integrator is None:raise Reject('Core fee data missing')
            # Fee ticks, NOT fee amounts: 1e6 = 100%; includes explicit integrator component.
            fee=number(t.usd_amount)*(number(rate)+number(integrator))/Decimal(1000000)
            out.append(dict(id=str(t.trade_id),quantity=str(number(t.size,True)),price=str(number(t.price,True)),
                            fee=str(fee),fee_currency='USDC'))
        if len({r['id'] for r in out})!=len(out):raise Reject('duplicate Core trade ids')
        return out

    async def submit(self, order):
        self.authority.check(self.identity,self.market.symbol,self.market.market_id,order)
        self.market.validate(order.quantity,order.price)
        n=self._mapped(order.client_id,create=True)
        # Before any new signature fetch authoritative nextNonce. Never retry a reserved ID.
        raw=self._ok(await self._call(self.signer.tx_api.next_nonce(account_index=int(self.identity.account),
                                                        api_key_index=self.identity.api_key_index)))
        nonce=self.ledger.reserve(order,raw.nonce)
        base=order.quantity/self.market.size_step;px=order.price/self.market.price_step
        if base!=base.to_integral_value() or px!=px.to_integral_value() or not 0 < px < 2**32 or not 0 < base < 2**63:
            raise Reject('Core integer price/base units overflow')
        try:
            tx_type,tx_info,tx_hash,error=self.signer.sign_create_order(market_index=self.market.market_id,
                client_order_index=n,base_amount=int(base),price=int(px),is_ask=order.side=='sell',
                order_type=0 if order.kind=='LIMIT' else 1,time_in_force=2 if order.kind=='LIMIT' else 0,
                reduce_only=order.reduce_only,trigger_price=0,order_expiry=-1 if order.kind=='LIMIT' else 0,
                integrator_account_index=0,integrator_taker_fee=0,integrator_maker_fee=0,
                self_trade_behavior_mode=0,self_trade_equality_mode=0,skip_nonce=0,
                nonce=nonce,api_key_index=self.identity.api_key_index)
            if error or not tx_info:raise ValueError('signing failed')
            raw=await self._call(self.signer.tx_api.send_tx(tx_type=tx_type,tx_info=tx_info))
            self._ok(raw)
            self.ledger.status(order.client_id,'ACK_ONLY')
            return dict(status='ACK_ONLY',filled_base=None,client_order_index=n,tx_hash=tx_hash)
        except Exception:
            self.ledger.status(order.client_id,'UNKNOWN')
            return dict(status='UNKNOWN',filled_base=None,client_order_index=n)

    def close(self):
        self.ledger.close()
