"""Linux local signer isolation. No HTTP registration or generic signing RPC.

Trusted parent/child use bounded JSON frames, credentials travel once through an
anonymous pipe, never argv/environment/disk. This is process isolation, NOT an OS
sandbox: same-user/root/dependency compromise remains out of scope. Python/SDK
immutable copies cannot be reliably erased; kill fences active authority.
"""
import asyncio
import ctypes
from dataclasses import dataclass
import json
import multiprocessing as mp
import os
import resource
import signal
import threading
import time
from types import SimpleNamespace
from .domain import Reject, number
from .live import Authority, Order
from .keystore import MAX_SECRET_BYTES, _decode_secret

ACK='ARM EXACT SCOPED ORDERS'
LIMIT=1024*1024


@dataclass(frozen=True)
class SessionSpec:
    identity: object
    symbol: str
    asset_id: int
    max_quantity: str
    max_notional: str
    max_slippage_bps: str
    journal: str
    artifacts: str
    fee_rate_cap: str | None = None

    def __post_init__(self):
        if self.identity.deployment=='extended-mainnet-starknet':
            if self.fee_rate_cap is None or not 0<=number(self.fee_rate_cap)<=1:
                raise Reject('Extended requires explicit fractional fee rate cap')
        elif self.fee_rate_cap is not None:
            raise Reject('fee rate cap only supported for Extended')

    def authority(self):
        return Authority(self.identity,self.symbol,self.asset_id,self.max_quantity,self.max_notional,self.max_slippage_bps)


def _send(conn, value):
    raw=json.dumps(value,separators=(',',':')).encode()
    if len(raw)>LIMIT:raise Reject('signer frame too large')
    conn.send_bytes(raw)


def _receive(conn):
    return json.loads(conn.recv_bytes(LIMIT))


def _child(conn, spec, factory, until, parent_pid, read_only=False):
    # Fail closed on unsupported OS. Block SDK stdout/stderr and core dumps.
    try:
        resource.setrlimit(resource.RLIMIT_CORE,(0,0))
        libc=ctypes.CDLL(None,use_errno=True)
        if libc.prctl(1,signal.SIGKILL)!=0 or libc.prctl(4,0)!=0:raise RuntimeError()
        if os.getppid()!=parent_pid:os._exit(1)
        fd=os.open(os.devnull,os.O_WRONLY)
        os.dup2(fd,1);os.dup2(fd,2);os.close(fd)
        timer=threading.Timer(max(0,until-time.monotonic()),lambda:os._exit(0))
        timer.daemon=True;timer.start()
        secret=bytearray(conn.recv_bytes(MAX_SECRET_BYTES))
        async def run_bridge(bridge,snapshot):
            if not read_only:
                bridge.authority.arm(ACK,lifetime=max(1,min(900,int(until-time.monotonic()))))
            m=bridge.market
            if (bridge.identity!=spec.identity or (m.symbol,m.market_id)!=(spec.symbol,spec.asset_id)):
                raise Reject('factory scope mismatch')
            _send(conn,dict(ok=True,market=dict(symbol=m.symbol,market_id=m.market_id,size_step=str(m.size_step))))
            while time.monotonic()<until:
                request=_receive(conn)
                try:
                    if not isinstance(request,dict) or set(request)!={'method','arg'}:raise Reject('typed RPC required')
                    method,arg=request['method'],request['arg']
                    if read_only and method not in ('positions','open_orders','registration'):
                        raise Reject('read-only signer forbids execution')
                    if method=='submit':
                        if not isinstance(arg,dict) or set(arg)!=set(Order.__dataclass_fields__):raise Reject('typed order required')
                        order=Order(**arg)
                        bridge.authority.check(spec.identity,spec.symbol,spec.asset_id,order)
                        # Guard is implemented below; hydration tests never contact network.
                        validate_snapshot(bridge,snapshot(),order)
                        result=await bridge.submit(order)
                        # ACK only, never return arbitrary SDK payload/tx signature/auth.
                        result={k:result[k] for k in ('status','filled_base','order_id','client_order_index') if k in result}
                    elif method=='registration' and arg is None:
                        await bridge.registration()
                        result=True
                    elif method in ('positions','open_orders') and arg is None:
                        result=await getattr(bridge,method)()
                        if method=='positions':result=[dict(symbol=r['symbol'],quantity=str(number(r['quantity']))) for r in result]
                        else:result=[{} for _ in result]  # worker needs only emptiness
                    elif method=='order_status' and isinstance(arg,str) and len(arg)<=80:
                        result=await bridge.order_status(arg)
                        result={k:result[k] for k in ('status','order_id','filled_base') if k in result}
                    elif method=='fills' and type(arg) in (int,str):
                        rows=await bridge.fills(arg)
                        fee_currency={'extended-mainnet-starknet':'USD','aster-mainnet':'USDT'}.get(spec.identity.deployment,'USDC')
                        result=[dict(id=str(r['id']),quantity=str(number(r['quantity'],True)),price=str(number(r['price'],True)),
                                     fee=str(number(r['fee'])),fee_currency=fee_currency) for r in rows if r['fee_currency']==fee_currency]
                        if len(result)!=len(rows):raise Reject('unsupported fee')
                        for source,fill in zip(rows,result):
                            if source.get('executed_at') is not None:
                                fill['executed_at']=str(number(source['executed_at'],True))
                    else:raise Reject('operation not allowlisted')
                    _send(conn,dict(ok=True,result=result))
                except Exception:
                    _send(conn,dict(ok=False))  # never serialize exception strings
        async def serve():
            try:
                credentials=_decode_secret(spec.identity,secret)
                bridge,snapshot=factory(spec,credentials)
            finally:
                for i in range(len(secret)):secret[i]=0
            del credentials
            try:
                await run_bridge(bridge,snapshot)
            finally:
                close=getattr(bridge,'close',None)
                if close is not None:
                    result=close()
                    if hasattr(result,'__await__'):await result
        asyncio.run(serve())
    except BaseException:
        try:_send(conn,dict(ok=False))
        except BaseException:pass
    finally:conn.close()


def validate_snapshot(bridge, snapshot, order):
    """Reject changed units, stale depth and unprotected/crossing orders."""
    market, book, _ = snapshot
    expected = bridge.market
    fields = ('symbol', 'market_id', 'base_asset', 'quote_asset', 'contract_type',
              'quantity_unit', 'multiplier', 'units_verified', 'size_step',
              'price_step', 'min_size', 'min_quote', 'order_quote_limit')
    if any(getattr(market, name) != getattr(expected, name) for name in fields):
        raise Reject('market metadata changed; review scope again')
    market.validate(order.quantity, order.price)
    depth = book.walk(order.side, order.quantity, time.time(), 3)
    bid, ask = book.sides['bids'][0][0], book.sides['asks'][0][0]
    midpoint = (bid + ask) / 2
    if abs(order.reference_price / midpoint - 1) * 10000 > bridge.authority.max_slippage_bps:
        raise Reject('reference price differs from fresh book')
    if order.kind == 'LIMIT':
        if (order.side == 'buy' and order.price >= ask) or (order.side == 'sell' and order.price <= bid):
            raise Reject('post-only limit crosses book')
    elif ((order.side == 'buy' and order.price < depth['worst_price'])
          or (order.side == 'sell' and order.price > depth['worst_price'])):
        raise Reject('protection price cannot cover visible target depth')
    if order.quantity * max(order.price, depth['worst_price']) > bridge.authority.max_notional:
        raise Reject('fresh depth exceeds notional scope')


class SignerSession:
    def __init__(self, vault, spec, *, factory=None):
        self.vault,self.spec=vault,spec
        self.identity=spec.identity;self.authority=spec.authority()
        self.factory=factory;self._process=None;self._conn=None;self._timer=None
        self._rpc_lock=threading.Lock()
    def __repr__(self):return '<SignerSession credentials=REDACTED>'
    def __enter__(self):return self
    def __exit__(self,*args):self.close();self.vault.lock()
    @property
    def pid(self):return self._process.pid if self._process else None
    @property
    def alive(self):return self._process is not None and self._process.is_alive()
    def unlock(self, password, *, confirmation, lifetime):
        self._unlock(password,confirmation=confirmation,lifetime=lifetime,read_only=False)
    def unlock_readonly(self, password, *, lifetime=60):
        self._unlock(password,confirmation=None,lifetime=lifetime,read_only=True)
    def _unlock(self, password, *, confirmation, lifetime, read_only):
        self.close()
        if type(lifetime) is not int or not 1<=lifetime<=900:raise Reject('bounded lifetime required')
        if not read_only:
            self.authority.arm(confirmation,lifetime=lifetime);self.authority.lock()
        try:
            self.vault.unlock(self.identity,password,lifetime=lifetime)
            del password
            until=time.monotonic()+lifetime
            factory=self.factory
            if factory is None:
                from .signer_factory import sdk_factory
                factory=sdk_factory
            context=mp.get_context('spawn')
            parent,child=context.Pipe(duplex=True);self._conn=parent
            self._process=context.Process(target=_child,args=(child,self.spec,factory,until,os.getpid(),read_only),daemon=True)
            self._process.start();child.close()
            self.vault.handoff(self.identity,parent,self.close)
            self._timer=threading.Timer(lifetime,self.close);self._timer.daemon=True;self._timer.start()
            if not parent.poll(min(30,lifetime)):raise Reject('signer startup timeout')
            response=_receive(parent)
            if response.get('ok') is not True:raise Reject('signer startup refused')
            self.market=SimpleNamespace(**response['market']);self.market.size_step=number(self.market.size_step,True)
            if not read_only:self.authority.arm(confirmation,lifetime=max(1,int(until-time.monotonic())))
            if not self.alive:raise Reject('signer expired')
        except BaseException:
            self.close();self.vault.lock();raise Reject('local signer unlock refused') from None
    def close(self):
        self.authority.lock()
        if self._timer:self._timer.cancel();self._timer=None
        p=self._process
        if p is not None:
            if p.is_alive():p.kill()
            p.join(timeout=3)
        if self._conn:
            self._conn.close();self._conn=None
    def _rpc(self,method,arg=None):
        with self._rpc_lock:
            try:
                if not self.alive or self._conn is None:raise Reject('inactive signer')
                _send(self._conn,dict(method=method,arg=arg))
                if not self._conn.poll(20):raise Reject('signer timeout')
                response=_receive(self._conn)
                if response.get('ok') is not True:raise Reject('signer refused')
                return response['result']
            except Exception:
                self.close();raise Reject('signer operation refused; reconciliation required') from None
    async def submit(self,order):
        if not isinstance(order,Order):raise Reject('typed order required')
        self.authority.check(self.identity,self.spec.symbol,self.spec.asset_id,order)
        return self._rpc('submit',{k:str(v) for k,v in order.__dict__.items()})
    async def positions(self):return self._rpc('positions')
    async def registration(self):return self._rpc('registration')
    async def open_orders(self):return self._rpc('open_orders')
    async def order_status(self,client_id):return self._rpc('order_status',client_id)
    async def fills(self,order_id):return self._rpc('fills',order_id)
