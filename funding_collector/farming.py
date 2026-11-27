"""Opt-in bounded durable round trips over the existing DN opening worker.

Exclusive accounts only. No cancel, corrective hedge, blind retry, infinite loop,
withdrawal or other capital operation. Restart never restores runtime authority.
"""
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
import random
import time
from .domain import Reject, number
from .live import Order
from .live_worker import Worker

SUPPORTED = ('hyperliquid-mainnet-native', 'lighter-core-mainnet', 'lighter-rh-mainnet')
POLICY_FIELDS = {'hold_seconds','min_delay_seconds','max_delay_seconds','max_cycles','max_turnover','max_fees'}


def validate_policy(p):
    if not isinstance(p, dict) or set(p) != POLICY_FIELDS:
        raise Reject('exact bounded farming policy required')
    for key, lo, hi in (('hold_seconds',60,86400),('min_delay_seconds',1,86400),
                        ('max_delay_seconds',1,86400),('max_cycles',1,10)):
        if type(p[key]) is not int or not lo <= p[key] <= hi:
            raise Reject('finite integer farming bounds required')
    if p['min_delay_seconds'] > p['max_delay_seconds']:
        raise Reject('minimum delay exceeds maximum')
    for key in ('max_turnover','max_fees'):
        if not isinstance(p[key],str): raise Reject('decimal budget string required')
        number(p[key],True)
    return dict(p)


class FarmingWorker(Worker):
    def __init__(self, *args, clock=time.time, choose_delay=random.SystemRandom().randint, **kwargs):
        super().__init__(*args, **kwargs)
        self.clock, self.choose_delay = clock, choose_delay
        self.db.execute('CREATE TABLE IF NOT EXISTS farming (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        self.db.commit()

    def farming_bytes(self, ident):
        row=self.db.execute('SELECT data FROM farming WHERE id=?',(ident,)).fetchone()
        if not row: raise Reject('unknown farming run')
        return row[0].encode()

    def farming_state(self, ident):
        s=json.loads(self.farming_bytes(ident))
        if s['version']!=1 or s['scope']!=self.scope: raise Reject('farming scope mismatch')
        return s

    def _farm_save(self,s,event=None):
        if event: s['journal'].append(dict(sequence=len(s['journal'])+1,event=event,at=self.clock()))
        self.db.execute('INSERT INTO farming VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                        (s['intent']['id'],json.dumps(s,sort_keys=True,allow_nan=False,separators=(',',':'))))
        self.db.commit()

    def add_farming(self,intent,policy):
        policy=validate_policy(policy)
        if any(v.identity.deployment not in SUPPORTED or v.authority.allow_close is not True
               for v in (self.primary,self.hedge)): raise Reject('farming close capability unavailable')
        existing=self.db.execute('SELECT data FROM farming WHERE id=?',(intent['id'],)).fetchone()
        if existing:
            s=self.farming_state(intent['id'])
            if s['intent']!=intent or s['policy']!=policy: raise Reject('farming idempotency conflict')
            return
        if self.db.execute('SELECT 1 FROM farming').fetchone(): raise Reject('one farming run per exclusive root')
        # Opening validation remains exactly the manual worker's validation.
        opening=dict(intent,id=self._cycle_id(intent['id'],1))
        super().add(opening)
        s=dict(version=1,scope=self.scope,intent=dict(intent),policy=policy,cycle=1,
               opening_id=opening['id'],status='RUNNING',phase='OPENING',journal=[],
               close_orders={},close_fills={'primary':[],'hedge':[]},
               fees_by_currency={},turnover_reserved='0',last_clock=self.clock())
        self._farm_save(s,'farming_created')

    @staticmethod
    def _cycle_id(ident,cycle):
        return 'farm-'+hashlib.sha256((ident+':'+str(cycle)).encode()).hexdigest()[:32]

    async def _positions(self,s):
        result={}
        for label,v in (('primary',self.primary),('hedge',self.hedge)):
            if await v.open_orders(): raise Reject('working orders block close/next cycle')
            quantity=Decimal(0)
            for row in await v.positions():
                q=number(row['quantity'])
                if q and row['symbol']!=v.market.symbol: raise Reject('unrelated position')
                quantity+=q
            result[label]=quantity
        return result

    def _expected(self,s,label):
        opened=self.state(s['opening_id'])
        sign=1 if s['intent']['side']=='buy' else -1
        if label=='hedge': sign=-sign
        closed=sum((number(f['quantity'],True) for f in s['close_fills'][label]),Decimal(0))
        return (number(opened[label+'_filled'])-closed)*sign

    async def _reconcile(self,s):
        opened=self.state(s['opening_id'])
        for label,v in (('primary',self.primary),('hedge',self.hedge)):
            status=await v.order_status(opened['orders'][label]['client_id'])
            if status['status'].lower()!='filled': raise Reject('opening no longer confirmed')
            rows=await v.fills(status['order_id'])
            current={f['id']:f for f in rows}
            previous={f['id']:f for f in opened['fills'][label]}
            if len(current)!=len(rows) or set(current)!=set(previous): raise Reject('opening fill identity changed')
            economic=lambda r:{k:v for k,v in r.items() if k!='executed_at'}
            for ident,old in previous.items():
                new=current[ident]
                if economic(old)!=economic(new): raise Reject('opening economic history changed')
                if 'executed_at' in new:
                    number(new['executed_at'],True)
                    if 'executed_at' in old and number(old['executed_at'])!=number(new['executed_at']):
                        raise Reject('opening execution time changed')
        positions=await self._positions(s)
        if any(positions[k]!=self._expected(s,k) for k in positions):
            raise Reject('actual holdings differ from strategy-owned fill ledger')
        return positions

    def _cycle_reservation(self,s):
        # Reserve both scoped maximum close notionals BEFORE opening. A close
        # must never be blocked halfway through because opening spent its budget.
        opening=number(s['intent']['quantity'])*(number(s['intent']['primary_price'])+number(s['intent']['hedge_price']))
        return opening+self.primary.authority.max_notional+self.hedge.authority.max_notional

    def _reserve_turnover(self,s,amount):
        total=number(s['turnover_reserved'])+amount
        if total>number(s['policy']['max_turnover']): raise Reject('turnover budget exceeded')
        s['turnover_reserved']=str(total)

    def _fees(self,s):
        fees={}
        opened=self.state(s['opening_id'])
        for rows in list(opened['fills'].values())+list(s['close_fills'].values()):
            for f in rows:
                currency=f['fee_currency']
                fees[currency]=fees.get(currency,Decimal(0))+number(f['fee'])
        for currency,value in s.get('previous_fees',{}).items():
            fees[currency]=fees.get(currency,Decimal(0))+number(value)
        s['fees_by_currency']={k:str(v) for k,v in fees.items()}

    async def _close_submit(self,s,label):
        positions=await self._reconcile(s)
        q=positions[label]
        if not q: raise Reject('unexpected zero strategy holding')
        v=self.primary if label=='primary' else self.hedge
        side='sell' if q>0 else 'buy'
        quote=await v.close_quote(side,str(abs(q)))
        ident='0x'+hashlib.sha256((s['opening_id']+label+':close').encode()).hexdigest()[:32]
        order=Order(ident,side,abs(q),quote['price'],quote['reference_price'],'MARKET',reduce_only=True)
        v.authority.check(v.identity,v.market.symbol,v.market.market_id,order)
        # The full maximum close notional was durably reserved before opening.
        if not s.get('opening_reserved'): raise Reject('close reservation missing')
        s['close_orders'][label]=dict(request={k:str(value) if isinstance(value,Decimal) else value for k,value in asdict(order).items()},status='PENDING')
        s['phase']='CLOSING_'+label.upper()
        self._farm_save(s,'write_ahead_close_'+label)
        self.checkpoint('CLOSE_'+label.upper()+'_PENDING')
        ack=await v.submit(order)
        self.checkpoint('CLOSE_'+label.upper()+'_SENT')
        s['close_orders'][label]['status']=ack['status']
        if ack['status'] in ('UNKNOWN','REJECTED'): s['status']='ATTENTION'
        self._farm_save(s,'close_'+label+'_ack_not_fill')

    async def _close_observe(self,s,label):
        v=self.primary if label=='primary' else self.hedge
        order=s['close_orders'][label]['request']
        status=await v.order_status(order['client_id'])
        if status['status']=='UNKNOWN': raise Reject('close outcome unknown; never resend')
        fills=await v.fills(status['order_id'])
        seen=set();total=Decimal(0)
        for f in fills:
            if f['id'] in seen or f['fee_currency']!='USDC': raise Reject('close fill identity/currency')
            seen.add(f['id']);total+=number(f['quantity'],True)
            number(f['price'],True);number(f['fee'])
            if 'executed_at' in f: number(f['executed_at'],True)
        current={f['id']:f for f in fills}
        for old in s['close_fills'][label]:
            new=current.get(old['id'])
            economic=lambda r:{k:v for k,v in r.items() if k!='executed_at'}
            if new is None or economic(old)!=economic(new): raise Reject('close fill history changed')
            if 'executed_at' in old:
                if 'executed_at' in new and number(new['executed_at'])!=number(old['executed_at']): raise Reject('close time changed')
                new['executed_at']=old['executed_at']
        target=number(order['quantity'],True)
        if total>target: raise Reject('close overfill')
        s['close_fills'][label]=fills
        self._farm_save(s,'close_'+label+'_confirmed_trades')
        complete=status['status'].lower()=='filled' and total==target
        if status['status'].lower() not in ('open','pending') and not complete:
            raise Reject('partial/dust/terminal close; manual attention required')
        return complete

    def _fresh_clock(self,s):
        now=self.clock()
        number(now,True)
        if now<s['last_clock']: raise Reject('wall clock moved backward')
        s['last_clock']=now
        return now

    async def tick_farming(self,ident):
        s=self.farming_state(ident)
        if not self.armed or s['status'] in ('DONE','ATTENTION'): return s
        try:
            # Runtime authority is never persisted/restored. Clock regressions cannot
            # silently extend/re-trigger a durable holding or delay deadline.
            for v in (self.primary,self.hedge):
                if time.monotonic()>=v.authority._until: raise Reject('runtime authorization expired')
            now=self._fresh_clock(s)
            if s['phase']=='OPENING':
                if not s.get('opening_reserved'):
                    self._reserve_turnover(s,self._cycle_reservation(s))
                    s['opening_reserved']=True
                    self._farm_save(s,'opening_turnover_reserved')
                opened=await super().tick(s['opening_id'])
                if opened['status'] in ('ATTENTION','UNKNOWN'): raise Reject('opening requires attention')
                if opened['status']=='COMPLETED_CONFIRMED':
                    await self._reconcile(s)
                    s.update(phase='HOLDING',hold_deadline=self._fresh_clock(s)+s['policy']['hold_seconds'])
                    self._farm_save(s,'both_legs_hedged_hold_started')
            elif s['phase']=='HOLDING':
                if now>=s['hold_deadline']: await self._close_submit(s,'primary')
            elif s['phase']=='CLOSING_PRIMARY':
                if await self._close_observe(s,'primary'): await self._close_submit(s,'hedge')
            elif s['phase']=='CLOSING_HEDGE':
                if await self._close_observe(s,'hedge'):
                    positions=await self._reconcile(s)
                    if any(positions.values()): raise Reject('not flat')
                    self._fees(s)
                    if sum((number(v) for v in s['fees_by_currency'].values()),Decimal(0))>number(s['policy']['max_fees']):
                        raise Reject('observed fee budget exceeded; no next cycle')
                    if s['cycle']>=s['policy']['max_cycles']:
                        s.update(status='DONE',phase='DONE')
                        self._farm_save(s,'both_legs_confirmed_flat_done')
                    else:
                        # Refuse another opening while flat, before scheduling it.
                        amount=self._cycle_reservation(s)
                        if number(s['turnover_reserved'])+amount>number(s['policy']['max_turnover']):
                            raise Reject('no next cycle within turnover budget')
                        delay=self.choose_delay(s['policy']['min_delay_seconds'],s['policy']['max_delay_seconds'])
                        if type(delay) is not int or not s['policy']['min_delay_seconds']<=delay<=s['policy']['max_delay_seconds']:
                            raise Reject('invalid random delay')
                        s.update(phase='DELAY',chosen_delay=delay,delay_deadline=self._fresh_clock(s)+delay)
                        self._farm_save(s,'flat_random_delay_persisted')
            elif s['phase']=='DELAY':
                if now>=s['delay_deadline']:
                    if any((await self._positions(s)).values()): raise Reject('next cycle requires flat accounts')
                    s['cycle']+=1
                    s['opening_id']=self._cycle_id(ident,s['cycle'])
                    opening=dict(s['intent'],id=s['opening_id'])
                    super().add(opening)
                    s['previous_fees']=dict(s['fees_by_currency'])
                    s.update(phase='OPENING',opening_reserved=False,close_orders={},close_fills={'primary':[],'hedge':[]})
                    s.pop('hold_deadline',None)
                    self._farm_save(s,'next_bounded_cycle')
            else: raise Reject('unknown farming phase')
            self._fees(s)
            self._farm_save(s)
        except Exception:
            s.update(status='ATTENTION',reason='Reconciliation/authority/transport failure; no resend, cancellation or flatten')
            self._farm_save(s,'farming_requires_attention')
        return s
