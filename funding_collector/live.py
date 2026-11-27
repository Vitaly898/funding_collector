"""Private execution foundation. Not registered with HTTP/public/paper dispatch.

Local filters are defense in depth, NOT protocol permissions. Dedicated agents
and separately funded owners remain required. No approvals/transfer API exists.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
import fcntl
import json
import os
import re
import sqlite3
import time
from .domain import Reject, number

DEPLOYMENTS = {'hyperliquid-mainnet-native': ('https://api.hyperliquid.xyz', None),
               'entropy-mainnet-io': ('https://api.hyperliquid.xyz', None),
               'lighter-core-mainnet': ('https://mainnet.zklighter.elliot.ai', 304),
               'lighter-rh-mainnet': ('https://api.rh.lighter.xyz', 466324),
               'extended-mainnet-starknet': ('https://api.starknet.extended.exchange', None),
               'aster-mainnet': ('https://fapi.asterdex.com', 1666)}


@dataclass(frozen=True)
class Identity:
    deployment: str
    account: str
    signer: str
    api_key_index: int | None = None

    def __post_init__(self):
        if self.deployment not in DEPLOYMENTS or not isinstance(self.signer,str) or not 1 <= len(self.signer) <= 128:
            raise Reject('unsupported deployment or signer identity')
        if self.deployment in ('hyperliquid-mainnet-native','entropy-mainnet-io','aster-mainnet'):
            if (not all(re.fullmatch(r'0x[0-9a-fA-F]{40}', x) for x in (self.account,self.signer))
                    or self.account.lower()==self.signer.lower() or self.api_key_index is not None):
                raise Reject('dedicated HL agent and separate account required')
        elif self.deployment=='extended-mainnet-starknet':
            # API account id, not accountIndex or the signing vault id.
            if (not isinstance(self.account,str) or not re.fullmatch(r'0|[1-9][0-9]*',self.account)
                    or not re.fullmatch(r'0x[1-9a-f][0-9a-f]{0,63}',self.signer)
                    or self.api_key_index is not None):
                raise Reject('canonical Extended API account id and Stark public key required')
        elif (not self.account.isascii() or not self.account.isdigit() or str(int(self.account))!=self.account
              or type(self.api_key_index) is not int or not 4 <= self.api_key_index <= 254):
            raise Reject('Core account index and unreserved API key index required')

    @property
    def nonce_scope(self):
        # Protocol nonces are agent-wide: dedicated distinct keys are mandatory.
        return (self.deployment,self.account,self.signer,self.api_key_index)


def validate_private_pair(first, second):
    """Exact reviewed units only; identities never imply credential reuse."""
    a,b=first.identity,second.identity
    if a.signer.lower()==b.signer.lower():
        raise Reject('distinct dedicated execution keys required for each venue')
    if a.deployment==b.deployment:
        raise Reject('distinct deployments required')
    # Each exact contract scope is checked against fresh venue metadata separately.
    # Neither equal nor different tickers establish economic equivalence.


@dataclass(frozen=True)
class Order:
    client_id: str
    side: str
    quantity: Decimal
    price: Decimal
    reference_price: Decimal
    kind: str
    reduce_only: bool = False

    def __post_init__(self):
        if type(self.reduce_only) is not bool or (self.reduce_only and self.kind != 'MARKET'):
            raise Reject('reduce-only close requires typed protected IOC')
        if (not isinstance(self.client_id,str) or not re.fullmatch(r'0x[0-9a-f]{32}', self.client_id)
                or self.side not in ('buy','sell') or self.kind not in ('LIMIT','MARKET')):
            raise Reject('typed LIMIT/post-only or protected MARKET/IOC with 128-bit client id required')
        for key in ('quantity','price','reference_price'):
            object.__setattr__(self,key,number(getattr(self,key),True))


@dataclass
class Authority:
    identity: Identity
    symbol: str
    asset_id: int
    max_quantity: Decimal
    max_notional: Decimal
    max_slippage_bps: Decimal
    allow_close: bool = False
    _until: float = field(default=0,init=False,repr=False)

    def __post_init__(self):
        pattern=r'io:[A-Z0-9_.-]{1,30}' if self.identity.deployment=='entropy-mainnet-io' else r'[A-Za-z0-9][A-Za-z0-9_.-]{0,39}'
        valid_symbol=isinstance(self.symbol,str) and re.fullmatch(pattern,self.symbol)
        if not valid_symbol or type(self.asset_id) is not int or self.asset_id < 0:
            raise Reject('exact bounded market symbol and metadata asset id required')
        for key in ('max_quantity','max_notional','max_slippage_bps'):
            setattr(self,key,number(getattr(self,key),True))
        if self.max_slippage_bps > 100:
            raise Reject('foundation slippage ceiling is 100 bps')

    def arm(self, confirmation, *, lifetime):
        if confirmation != 'ARM EXACT SCOPED ORDERS' or type(lifetime) is not int or not 1 <= lifetime <= 900:
            raise Reject('explicit local arm and bounded lifetime required')
        self._until=time.monotonic()+lifetime

    def lock(self):
        self._until=0

    def require_read(self, identity):
        if identity != self.identity:
            raise Reject('account/deployment/agent mismatch')

    def check(self, identity, symbol, asset_id, order):
        self.require_read(identity)
        if time.monotonic() >= self._until:
            raise Reject('EXECUTION_DISABLED: explicit local runtime arm required')
        if (symbol,asset_id)!=(self.symbol,self.asset_id) or not isinstance(order,Order):
            raise Reject('market or typed intent mismatch')
        if order.reduce_only and (self.allow_close is not True or self.identity.deployment not in
                ('hyperliquid-mainnet-native','lighter-core-mainnet','lighter-rh-mainnet')):
            raise Reject('reduce-only close outside explicit supported scope')
        if order.quantity > self.max_quantity or order.quantity*order.price > self.max_notional:
            raise Reject('quantity/notional scope exceeded')
        if abs(order.price/order.reference_price-1)*10000 > self.max_slippage_bps:
            raise Reject('explicit reference/protection slippage exceeded')


class OrderLedger:
    """Write-ahead duplicate barrier + nonce audit, one local writer per scope.

    Pending rows are never auto-deleted/retried, including after process death.
    Use one exclusive journal directory per dedicated signer workload; the caller
    must not reuse its signer in other apps. sqlite FULL commits precede sending.
    """
    def __init__(self, root, identity):
        root=Path(root); root.mkdir(mode=0o700,parents=True,exist_ok=True)
        if root.is_symlink() or root.stat().st_mode & 0o077:
            raise Reject('private journal directory must be 0700')
        self.lock_fd=os.open(root/'writer.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try: fcntl.flock(self.lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock_fd);raise Reject('signer journal already owned') from None
        dbpath=root/'orders.sqlite3'
        if dbpath.is_symlink():
            self.close();raise Reject('symlink journal refused')
        fd=os.open(dbpath,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);os.close(fd)
        self.db=sqlite3.connect(dbpath)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS scope (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT, nonce INTEGER)')
        self.db.execute('CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, request TEXT NOT NULL, nonce INTEGER NOT NULL, status TEXT NOT NULL)')
        scope=json.dumps(identity.nonce_scope)
        row=self.db.execute('SELECT value FROM scope WHERE id=1').fetchone()
        if row and row[0]!=scope:
            self.close();raise Reject('journal account/deployment/signer mismatch')
        self.db.execute('INSERT OR IGNORE INTO scope VALUES (1,?,-1)',(scope,));self.db.commit()

    def reserve(self, order, nonce, *, monotonic=False):
        try:
            self.db.execute('BEGIN IMMEDIATE')
            if self.db.execute('SELECT 1 FROM orders WHERE id=?',(order.client_id,)).fetchone():
                raise Reject('client id already reserved; reconcile, never blind resubmit')
            if self.db.execute("SELECT 1 FROM orders WHERE status IN ('PENDING','UNKNOWN')").fetchone():
                raise Reject('unresolved signer outcome blocks new IDs; reconcile first')
            last=self.db.execute('SELECT nonce FROM scope WHERE id=1').fetchone()[0]
            if not monotonic and nonce<=last:
                raise Reject('nonce reused/regressed; reconcile before signing')
            nonce=max(nonce,last+1) if monotonic else nonce
            if type(nonce) is not int or nonce < 0:
                raise Reject('invalid explicit nonce')
            request=json.dumps({k:str(v) for k,v in order.__dict__.items()},sort_keys=True)
            self.db.execute('INSERT INTO orders VALUES (?,?,?,?)',(order.client_id,request,nonce,'PENDING'))
            self.db.execute('UPDATE scope SET nonce=? WHERE id=1',(nonce,));self.db.commit()
            return nonce
        except BaseException:
            self.db.rollback();raise

    def status(self, client_id, status):
        self.db.execute('UPDATE orders SET status=? WHERE id=?',(status,client_id));self.db.commit()

    def close(self):
        if hasattr(self,'db'):self.db.close()
        if getattr(self,'lock_fd',None) is not None:
            os.close(self.lock_fd);self.lock_fd=None
