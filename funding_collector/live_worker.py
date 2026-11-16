"""Durable DN full-LIMIT -> ONE protected IOC worker foundation.

Not the paper Store; no old-state conversion. Disabled until local scoped arm,
not exposed in browser. No timeout for accumulation, cancellation, reprice,
Stop control, automatic additional hedge or flatten. Incomplete/unknown results
remain visible. Current foundation requires a flat, exclusive account baseline.
Local supervised CLI and isolated signer hydration live in supervised.py.
Independent security review and authorized live acceptance remain required.
"""
import asyncio
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
import time
from .domain import Reject,number
from .live import Order,OrderLedger,validate_private_pair

MODE='DN_PRIVATE_FULL_LIMIT_ONE_IOC_V1'
TERMINAL={'COMPLETED_CONFIRMED','ATTENTION'}


class Worker:
    def __init__(self, root, primary, hedge, *, checkpoint=None):
        from types import SimpleNamespace
        validate_private_pair(*(SimpleNamespace(identity=v.identity,symbol=v.market.symbol) for v in (primary,hedge)))
        self.primary,self.hedge=primary,hedge
        self.ledger=OrderLedger(root,primary.identity)
        self.db=self.ledger.db
        self.db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL)');self.db.commit()
        self.checkpoint=checkpoint or (lambda stage: None)
        self.armed=False
        self.scope=[dict(identity=asdict(v.identity),symbol=v.market.symbol,asset_id=v.market.market_id,
                         max_quantity=str(v.authority.max_quantity),max_notional=str(v.authority.max_notional),
                         max_slippage_bps=str(v.authority.max_slippage_bps)) for v in (primary,hedge)]

    def __enter__(self):return self
    def __exit__(self,*args):
        self.primary.authority.lock();self.hedge.authority.lock();self.ledger.close()

    def arm(self, confirmation, *, lifetime):
        self.primary.authority.arm(confirmation,lifetime=lifetime)
        self.hedge.authority.arm(confirmation,lifetime=lifetime)
        self.armed=True

    def add(self, intent):
        required={'id','quantity','side','primary_price','hedge_price','primary_reference','hedge_reference'}
        if (not isinstance(intent,dict) or set(intent)!=required or not isinstance(intent['id'],str)
                or not 1<=len(intent['id'])<=80 or intent['side'] not in ('buy','sell')):
            raise Reject('exact bounded worker intent required')
        quantity=number(intent['quantity'],True)
        for v in (self.primary,self.hedge):
            if quantity%v.market.size_step or quantity>v.authority.max_quantity:
                raise Reject('exact cross-venue base precision/quantity mismatch')
        for side in ('primary','hedge'):
            number(intent[side+'_price'],True);number(intent[side+'_reference'],True)
        old=self.db.execute('SELECT data FROM tasks WHERE id=?',(intent['id'],)).fetchone()
        if old:
            row=json.loads(old[0])
            if row['intent']!=intent or row['scope']!=self.scope:raise Reject('idempotency conflict or account/policy changed')
            return
        if self.db.execute("SELECT 1 FROM tasks WHERE json_extract(data,'$.status') NOT IN ('COMPLETED_CONFIRMED')").fetchone():
            raise Reject('one exclusive unresolved task per worker/account')
        # Closed tasks still leave positions. New tasks will fail the flat baseline.
        state=dict(version=1,mode=MODE,intent=intent,scope=self.scope,status='NEW',
                   phase='BASELINE',primary_filled='0',hedge_filled='0',residual_base='0',
                   fees_usdc='0',fills={'primary':[],'hedge':[]},journal=[],orders={})
        self._save(state,'created')

    def state(self, ident):
        row=self.db.execute('SELECT data FROM tasks WHERE id=?',(ident,)).fetchone()
        if not row:raise Reject('unknown private task')
        state=json.loads(row[0])
        if state['mode']!=MODE or state['version']!=1 or state['scope']!=self.scope:
            raise Reject('private state version/account/policy mismatch; no paper migration')
        return state

    def bytes(self, ident):
        return self.db.execute('SELECT data FROM tasks WHERE id=?',(ident,)).fetchone()[0].encode()

    def _save(self,s,event=None):
        if event:s['journal'].append(dict(sequence=len(s['journal'])+1,event=event,at=time.time()))
        self.db.execute('INSERT INTO tasks VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                        (s['intent']['id'],json.dumps(s,sort_keys=True,separators=(',',':'))));self.db.commit()

    async def _baseline(self, s, *, before_hedge=False):
        target=number(s['intent']['quantity'])
        for label,v in (('primary',self.primary),('hedge',self.hedge)):
            positions=await v.positions();orders=await v.open_orders()
            expected=(target if s['intent']['side']=='buy' else -target) if before_hedge and label=='primary' else Decimal(0)
            actual=Decimal(0)
            for p in positions:
                q=number(p['quantity'])
                if q and p.get('symbol')!=v.market.symbol:raise Reject('unrelated account inventory')
                actual+=q
            if actual!=expected or orders:raise Reject('positions/open-order baseline mismatch; no new order')
        self._save(s,'reconciled_before_hedge' if before_hedge else 'reconciled_flat_baseline')

    def _order(self,s,label):
        i=s['intent'];primary=label=='primary'
        # Include exact account scope in ID, never random regeneration on recovery.
        ident='0x'+hashlib.sha256((json.dumps(s['scope'],sort_keys=True)+i['id']+label).encode()).hexdigest()[:32]
        return Order(ident,i['side'] if primary else ('sell' if i['side']=='buy' else 'buy'),
                     number(i['quantity'] if primary else s['primary_filled']),
                     number(i[label+'_price']),number(i[label+'_reference']),'LIMIT' if primary else 'MARKET')

    async def _submit(self,s,label):
        venue=self.primary if label=='primary' else self.hedge
        order=self._order(s,label)
        venue.authority.check(venue.identity,venue.market.symbol,venue.market.market_id,order)
        s['orders'][label]=dict(client_id=order.client_id,status='PENDING')
        s['phase']=label.upper()+'_PENDING';s['status']='RUNNING'
        self._save(s,'write_ahead_'+label)
        self.checkpoint(s['phase'])
        ack=await venue.submit(order)
        # Injected crash probe only: exchange acceptance precedes durable ACK.
        self.checkpoint(label.upper()+'_SENT')
        s['orders'][label].update(ack)
        if ack['status']=='REJECTED':s.update(status='ATTENTION',reason=label+' rejected; no automatic flatten')
        elif ack['status']=='UNKNOWN':s.update(status='UNKNOWN',reason=label+' submission outcome unknown; reconciliation only')
        self._save(s,label+'_ack_not_fill')

    async def _observe(self,s,label):
        venue=self.primary if label=='primary' else self.hedge
        o=s['orders'][label]
        status=await venue.order_status(o['client_id'])
        if status['status']=='UNKNOWN':
            s.update(status='UNKNOWN',reason=label+' order unknown; no resubmission')
            self._save(s,'unknown_'+label);return False
        oid=status.get('order_id')
        if oid is None:raise Reject('order status lacks authoritative order id')
        fills=await venue.fills(oid)
        seen=set();total=Decimal(0)
        for f in fills:
            expected_currency='USD' if venue.identity.deployment=='extended-mainnet-starknet' else 'USDC'
            if f['id'] in seen or f['fee_currency']!=expected_currency:raise Reject('invalid fee/fill identity')
            seen.add(f['id']);total+=number(f['quantity'],True)
            number(f['price'],True);number(f['fee'])
        previous={f['id']:f for f in s['fills'][label]}
        current={f['id']:f for f in fills}
        if any(current.get(k)!=v for k,v in previous.items()):raise Reject('fill history lost/changed; reconcile manually')
        target=number(s['intent']['quantity'] if label=='primary' else s['primary_filled'])
        if total>target:raise Reject('confirmed order overfill; no automatic flatten')
        s['fills'][label]=fills;s[label+'_filled']=str(total)
        direction = Decimal(1) if s['intent']['side'] == 'buy' else Decimal(-1)
        s['residual_base']=str(direction*(number(s['primary_filled'])-number(s['hedge_filled'])))
        currencies={}
        for rows in s['fills'].values():
            for f in rows:
                currency=f['fee_currency']
                currencies[currency]=currencies.get(currency,Decimal(0))+number(f['fee'])
        s['fees_by_currency']={k:str(v) for k,v in currencies.items()}
        s['fees_usdc']=str(currencies.get('USDC',Decimal(0)))
        state=status['status'].lower()
        # A terminal status alone never establishes a fill. Individual trades are required.
        complete=state=='filled' and total==target
        if state=='filled' and not complete:
            s.update(status='ATTENTION',reason='filled status disagrees with confirmed trade ledger')
        elif state in ('rejected','canceled','cancelled','expired') or 'canceled' in state:
            s.update(status='ATTENTION',reason=label+' terminal incomplete; no extra hedge or flatten')
        elif complete:s['status']='RUNNING'
        self._save(s,label+'_confirmed_trades')
        self.checkpoint(label.upper()+'_FILLS_SAVED')
        return complete

    async def tick(self, ident):
        s=self.state(ident)
        if not self.armed or s['status'] in TERMINAL:return s
        try:
            if s['phase']=='BASELINE':
                await self._baseline(s)
                await self._submit(s,'primary')
                return s
            if s['phase']=='PRIMARY_PENDING':
                if not await self._observe(s,'primary') or s['status'] in TERMINAL:return s
                await self._baseline(s,before_hedge=True)
                await self._submit(s,'hedge')
                return s
            if s['phase']=='HEDGE_PENDING':
                if await self._observe(s,'hedge') and s['status'] not in TERMINAL:
                    s.update(status='COMPLETED_CONFIRMED',phase='COMPLETE')
                    self._save(s,'completed_confirmed_positions_remain_open')
                return s
            raise Reject('invalid private task phase')
        except Exception:
            # Never persist exception strings from SDKs (may contain auth material).
            s.update(status='ATTENTION',reason='private reconciliation/authority/transport failure; no resubmit or flatten')
            self._save(s,'failure_requires_attention');return s

    async def serve(self, *, poll_seconds=1):
        if not 0.1<=poll_seconds<=60:raise Reject('invalid polling interval')
        while True:
            for (ident,) in self.db.execute('SELECT id FROM tasks').fetchall():
                await self.tick(ident)
            await asyncio.sleep(poll_seconds)
