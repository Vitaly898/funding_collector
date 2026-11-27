"""Local-only supervised native/Core DN service. Never imported by HTTP actions.

Public JSON configuration is not a secret transport. Activation is deliberately
interactive on every process start; no arm flag, environment loader or auto-restart.
"""
import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
import sys
from .domain import Reject, number
from .live import Identity, Order, validate_private_pair
from .signer_session import SessionSpec

DEFAULT_ROOT=Path.home()/'.local/share/funding-collector/live-native-core'
FIELDS={'identity','symbol','asset_id','max_quantity','max_notional','max_slippage_bps'}
INTENT={'id','quantity','side','primary_price','hedge_price','primary_reference','hedge_reference'}
PRIVATE_CAPABILITIES={'entropy-mainnet-io':dict(
    implementation='official HL SDK scoped bridge; local supervised CLI only',
    dex='io',reviewed_symbol='io:NVDA',countervenues=['lighter-core-mainnet','lighter-rh-mainnet'],
    order_modes=['LIMIT_POST_ONLY','PROTECTED_MARKET_IOC'],automatic_wallet_sharing=False,
    dedicated_agent_name='funding-collector:entropy:io',registration_protocol_scope='owner account; NOT dex-limited',
    account_acceptance_verified=False)}


@dataclass(frozen=True)
class Config:
    specs: tuple
    intent: dict
    lifetime: int
    poll_seconds: float
    farming: dict | None = None


def load_config(path, root):
    raw=Path(path).read_bytes()
    if len(raw)>16384:raise Reject('oversized public config')
    c=json.loads(raw)
    if not isinstance(c,dict) or set(c) not in ({'version','legs','intent','lifetime','poll_seconds'}, {'version','legs','intent','lifetime','poll_seconds','farming'}) or c['version']!=1:
        raise Reject('exact versioned public config required')
    if type(c['lifetime']) is not int or not 1<=c['lifetime']<=900:raise Reject('bounded unlock required')
    if type(c['poll_seconds']) not in (float,int) or not 0.1<=c['poll_seconds']<=60:raise Reject('bounded polling required')
    if not isinstance(c['legs'],list) or len(c['legs'])!=2:raise Reject('two scoped legs required')
    farming=None
    if 'farming' in c:
        from .farming import validate_policy, SUPPORTED
        farming=validate_policy(c['farming'])
    specs=[]
    for n,leg in enumerate(c['legs']):
        if not isinstance(leg,dict):raise Reject('exact public leg fields required')
        extended=leg.get('identity',{}).get('deployment')=='extended-mainnet-starknet'
        if set(leg)!=(FIELDS|{'fee_rate_cap'} if extended else FIELDS):raise Reject('exact public leg fields required')
        spec=SessionSpec(**dict(leg,identity=Identity(**leg['identity']),
            journal=str(Path(root)/f'signer-{n}'),artifacts=str(Path(root)/f'public-{n}'),allow_close=farming is not None))
        if farming and spec.identity.deployment not in SUPPORTED: raise Reject('farming close unavailable on venue')
        spec.authority()
        specs.append(spec)
    validate_private_pair(*specs)
    i=c['intent']
    if (not isinstance(i,dict) or set(i)!=INTENT or any(not isinstance(v,str) for v in i.values())
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',i['id'])):raise Reject('exact bounded intent required')
    for n,spec in enumerate(specs):
        label='primary' if n==0 else 'hedge'
        order=Order('0x'+'00'*16,i['side'] if n==0 else ('sell' if i['side']=='buy' else 'buy'),
                    i['quantity'],i[label+'_price'],i[label+'_reference'],'LIMIT' if n==0 else 'MARKET')
        a=spec.authority();a.arm('ARM EXACT SCOPED ORDERS',lifetime=1)
        a.check(spec.identity,spec.symbol,spec.asset_id,order);a.lock()
    return Config(tuple(specs),dict(i),c['lifetime'],float(c['poll_seconds']),farming)


def status(root):
    """Allowlisted read-only durable projection. No vault discovery/SDK import/write."""
    path=Path(root)/'worker'/'orders.sqlite3'
    result: dict=dict(mode='LOCAL_PRIVATE_DN_STATUS',worker_process_active=False,trading_enabled=False,credentials_loaded=False,
                actual_accounts_read=False,activation='FRESH_TERMINAL_ARM_REQUIRED',tasks=[],
                warning='Durable observations only; positions/orders may remain open. No automatic flatten.',
                private_capabilities=PRIVATE_CAPABILITIES)
    if not path.exists():return result
    if path.is_symlink():raise Reject('unsafe status path')
    import fcntl
    import os
    fd=os.open(path.parent/'writer.lock',os.O_RDONLY|os.O_NOFOLLOW)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
        except BlockingIOError:result['worker_process_active']=True
    finally:os.close(fd)
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
    try:
        for (raw,) in db.execute('SELECT data FROM tasks ORDER BY id LIMIT 100'):
            s=json.loads(raw)
            if s.get('status') not in {'NEW','RUNNING','UNKNOWN','ATTENTION','COMPLETED_CONFIRMED'}:raise Reject('invalid worker status')
            if s.get('phase') not in {'BASELINE','PRIMARY_PENDING','HEDGE_PENDING','COMPLETE'}:raise Reject('invalid worker phase')
            row={k:s[k] for k in ('status','phase')}
            row.update({k:str(number(s[k])) for k in ('primary_filled','hedge_filled','residual_base','fees_usdc')})
            row['fees_by_currency']={k:str(number(v)) for k,v in s.get('fees_by_currency',{'USDC':s['fees_usdc']}).items() if k in ('USD','USDC','USDT')}
            row['event_count']=len(s['journal']);row['possible_open_orders_or_unobserved_fills']=s['phase']!='COMPLETE'
            result['tasks'].append(row)
    finally:db.close()
    return result


def configure(commands):
    p=commands.add_parser('live',help='local supervised native/Core service; default disabled')
    actions=p.add_subparsers(dest='live_action',required=True)
    for name in ('check','status','setup','account-check','unlock'):
        sub=actions.add_parser(name)
        sub.add_argument('--root',type=Path,default=DEFAULT_ROOT,help='separate private ledger/custody root')
        if name!='status':sub.add_argument('--config',type=Path,required=True,help='public identities, exact markets, targets and protection; no secrets')
        if name=='check':sub.add_argument('--public',action='store_true',help='explicit credential-free market/depth reads')


def public_snapshot(spec):
    # Public adapter receives no account, signer, or credential.
    if spec.identity.deployment=='hyperliquid-mainnet-native':
        from .hyperliquid import HyperliquidPublicAdapter
        adapter=HyperliquidPublicAdapter(spec.artifacts)
    elif spec.identity.deployment=='entropy-mainnet-io':
        from .entropy import EntropyPublicAdapter
        adapter=EntropyPublicAdapter(spec.artifacts)
    elif spec.identity.deployment=='extended-mainnet-starknet':
        from .extended import ExtendedPublicAdapter
        from .signer_factory import extended_snapshot
        return extended_snapshot(ExtendedPublicAdapter(spec.artifacts),spec)
    elif spec.identity.deployment=='aster-mainnet':
        from .aster import AsterPublicAdapter
        from .signer_factory import aster_snapshot
        return aster_snapshot(AsterPublicAdapter(spec.artifacts),spec)
    else:
        from .lighter import LighterPublicAdapter
        adapter=LighterPublicAdapter('rh' if spec.identity.deployment=='lighter-rh-mainnet' else 'core',spec.artifacts)
    return adapter.snapshot(spec.symbol)


def validate_public(c, *, reader=None):
    from types import SimpleNamespace
    from .signer_session import validate_snapshot
    markets=[]
    for n,spec in enumerate(c.specs):
        sample=(reader or public_snapshot)(spec);market=sample[0]
        markets.append(market)
        base='NVDA' if spec.identity.deployment=='entropy-mainnet-io' and spec.symbol=='io:NVDA' else spec.symbol
        if ((market.symbol,market.market_id)!=(spec.symbol,spec.asset_id)
                or market.base_asset!=base or market.quote_asset!='USD'
                or market.quantity_unit!='base' or market.contract_type!='linear_perpetual'
                or market.multiplier!=1 or not market.units_verified):raise Reject('public units/scope mismatch')
        label='primary' if n==0 else 'hedge';i=c.intent
        order=Order('0x'+'00'*16,i['side'] if n==0 else ('sell' if i['side']=='buy' else 'buy'),
                    i['quantity'],i[label+'_price'],i[label+'_reference'],'LIMIT' if n==0 else 'MARKET')
        validate_snapshot(SimpleNamespace(market=market,authority=spec.authority()),sample,order)
    if any(s.identity.deployment=='entropy-mainnet-io' for s in c.specs):
        from .adapters import Connection,Deployment
        from .entropy import validate_pair
        entries=[]
        for spec,market in zip(c.specs,markets):
            dep=spec.identity.deployment
            entropy=dep=='entropy-mainnet-io'
            deployment=Deployment('hyperliquid-mainnet-io' if entropy else 'rh' if dep=='lighter-rh-mainnet' else 'core',
                                  'entropy' if entropy else 'lighter')
            entries.append((Connection(dep,deployment,'private-explicit','private-explicit'),market))
        validate_pair(entries)


def open_sessions(c, root, *, session_factory=None):
    """Validate ALL encrypted public identities before creating ANY signer."""
    from .keystore import KeyStore
    from .signer_session import SignerSession
    paths=[Path(root)/f'vault-{n}' for n in range(2)]
    if any(not (p/'key.json').is_file() for p in paths):raise Reject('both encrypted vaults required')
    vaults=[KeyStore(p) for p in paths]
    if any(v.identity()!=s.identity for v,s in zip(vaults,c.specs)):raise Reject('vault public identity mismatch')
    return [(session_factory or SignerSession)(v,s) for v,s in zip(vaults,c.specs)]


EVENTS={'created','reconciled_flat_baseline','reconciled_before_hedge','write_ahead_primary',
        'write_ahead_hedge','primary_ack_not_fill','hedge_ack_not_fill','unknown_primary','unknown_hedge',
        'primary_confirmed_trades','hedge_confirmed_trades','completed_confirmed_positions_remain_open',
        'failure_requires_attention'}


def log_event(event, **fields):
    # Caller supplies fixed event codes/numbers only; never SDK strings, config or identity.
    print(json.dumps(dict(event=event,**fields),sort_keys=True),flush=True)


async def supervise(worker, c, sessions):
    import asyncio
    import time
    deadline=time.monotonic()+c.lifetime
    seen=0
    try:
        while True:
            if time.monotonic()>=deadline or not all(s.alive for s in sessions):
                worker.armed=False
                log_event('unlock_expired_submissions_disabled',possible_remaining_exposure=True,
                          possible_open_orders=True,automatic_flatten=False)
                return 0
            state=await worker.tick(c.intent['id'])
            for entry in state['journal'][seen:]:
                if entry['event'] in EVENTS:
                    log_event(entry['event'],sequence=entry['sequence'],status=state['status'],phase=state['phase'])
            seen=len(state['journal'])
            if state['status'] in {'COMPLETED_CONFIRMED','ATTENTION','UNKNOWN'}:
                log_event('worker_session_finished',status=state['status'],
                          possible_remaining_exposure=True,automatic_flatten=False)
                return 0
            await asyncio.sleep(c.poll_seconds)
    finally:
        worker.armed=False
        for session in sessions:session.close();session.vault.lock()
        log_event('authority_closed',possible_remaining_exposure=True,possible_open_orders=True,automatic_flatten=False)


def setup_secret(identity, masked):
    if identity.deployment=='extended-mainnet-starknet':
        from .extended_private import ExtendedCredentials
        api_key=masked('Extended API key:')
        private_key=masked('Extended Stark execution private key:')
        public_key=masked('Extended Stark public key:')
        raw_vault=masked('Extended signing vault id:')
        if not re.fullmatch(r'0|[1-9][0-9]{0,19}',raw_vault):raise Reject('invalid Extended vault id')
        return ExtendedCredentials(api_key=api_key,stark_private_key=private_key,
            stark_public_key=public_key,vault=int(raw_vault))
    return masked('Dedicated execution key:')


def interactive(args, c, *, reader=None, session_factory=None, checkpoint=None):
    import asyncio
    from contextlib import ExitStack
    import getpass
    import os
    import resource
    import signal
    from .keystore import KeyStore
    from .signer_session import ACK
    from .live_worker import Worker
    if args.live_action=='unlock' and c.farming:
        raise Reject('farming execution requires browser service; CLI does not support cycles')
    if not sys.stdin.isatty() or not sys.stderr.isatty():raise Reject('local terminal required')
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    # Fail closed rather than letting getpass fall back to echoed stdin.
    import warnings
    def masked(prompt):
        with warnings.catch_warnings():
            warnings.simplefilter('error',getpass.GetPassWarning)
            return getpass.getpass(prompt+' ')
    root=Path(args.root)
    if root.exists() and (root.is_symlink() or root.stat().st_mode&0o077 or root.stat().st_uid!=os.getuid()):
        raise Reject('private root must be owned 0700')
    if args.live_action=='setup':
        root.mkdir(mode=0o700,parents=True,exist_ok=True)
        for n,spec in enumerate(c.specs):
            vault=KeyStore(root/f'vault-{n}')
            if (vault.root/'key.json').exists():
                if vault.identity()!=spec.identity:raise Reject('existing vault public identity mismatch')
                continue
            print('Setup primary' if n==0 else 'Setup hedge',flush=True)
            secret=setup_secret(spec.identity,masked)
            password=masked('New local passphrase:')
            try:
                if password!=masked('Repeat passphrase:'):raise Reject('passphrases differ')
                vault.create(spec.identity,secret,password)
            finally:del secret,password
        log_event('encrypted_setup_complete_execution_disabled');return 0
    # Local identity checks and public executable units/depth BEFORE any SDK init.
    sessions=open_sessions(c,root,session_factory=session_factory)
    if args.live_action=='account-check':
        # Separate non-arming RPC capability, no Worker and no submission RPC.
        async def read_accounts():
            rows=[]
            for session in sessions:
                await session.registration()
                positions=await session.positions()
                orders=await session.open_orders()
                rows.append(dict(registration_valid=True,position_count=len(positions),open_order_count=len(orders)))
            return rows
        with ExitStack() as stack:
            for session in sessions:stack.enter_context(session)
            for n,session in enumerate(sessions):
                password=masked('Read-only local passphrase for '+('primary:' if n==0 else 'hedge:'))
                try:session.unlock_readonly(password,lifetime=min(c.lifetime,120))
                finally:del password
            rows=asyncio.run(read_accounts())
            print(json.dumps(dict(mode='ACCOUNT_READ_ONLY',trading_enabled=False,actual_accounts_read=True,accounts=rows)),flush=True)
        return 0
    validate_public(c,reader=reader)
    print('Full primary LIMIT before one opposite protected IOC. Expiry does NOT cancel or flatten.',flush=True)
    if input('Type ARM EXACT SCOPED ORDERS: ')!=ACK:raise Reject('fresh local acknowledgment required')
    with ExitStack() as stack:
        for session in sessions:stack.enter_context(session)
        for n,session in enumerate(sessions):
            password=masked('Local passphrase for '+('primary:' if n==0 else 'hedge:'))
            try:session.unlock(password,confirmation=ACK,lifetime=c.lifetime)
            finally:del password
        # Both signers own independently bounded process/pipe authority.
        with Worker(root/'worker',*sessions,checkpoint=checkpoint) as worker:
            worker.add(c.intent)
            worker.arm(ACK,lifetime=c.lifetime)
            old=signal.getsignal(signal.SIGTERM)
            def terminate(signum,frame):raise KeyboardInterrupt()
            signal.signal(signal.SIGTERM,terminate)
            try:return asyncio.run(supervise(worker,c,sessions))
            except KeyboardInterrupt:
                log_event('supervisor_interrupted',possible_remaining_exposure=True,possible_open_orders=True,automatic_flatten=False)
                return 1
            finally:signal.signal(signal.SIGTERM,old)


def main(args):
    try:
        if args.live_action=='status':result=status(args.root)
        else:
            c=load_config(args.config,args.root)
            if args.live_action=='check':
                result=dict(trading_enabled=False,credentials_loaded=False,actual_accounts_read=False,
                            config_valid=True,public_market_checked=False,ready_to_arm=False,
                            next_step='local terminal setup/unlock; security review and authorized acceptance still required',
                            private_capabilities=PRIVATE_CAPABILITIES)
                if args.public:
                    validate_public(c)
                    result['public_market_checked']=True
            else:
                if not sys.stdin.isatty() or not sys.stderr.isatty():raise Reject('local terminal required')
                return interactive(args,c)
        print(json.dumps(result,sort_keys=True));return 0
    except (Reject,OSError,ValueError,TypeError,KeyError,ArithmeticError,sqlite3.Error):
        print('Local live operation refused; no automatic resubmit or flatten.',file=sys.stderr)
        return 1
