"""Single-owner browser boundary for the existing scoped signer and DN worker.

No SDK imports or exchange reads on construction/status. Never resumes authority.
The application lock is held until all lifecycle work has actually finished.
"""
import fcntl
import os
from pathlib import Path
import stat
import threading

from .domain import Reject

WARNING = 'possible_remaining_exposure: lock does not cancel orders or flatten positions'


class LiveService:
    def __init__(self, root, *, session_factory=None, public_reader=None):
        self._root = Path(os.path.abspath(root))
        self._factory = session_factory
        self._reader = public_reader
        self._meta = threading.RLock()
        self._operation = threading.Lock()
        self._closed = False
        self._checked = False
        self._running = False
        self._enabled = False
        self._config = None
        self._error = None
        self._state = None
        self._sessions = []
        self._epoch = 0
        self._thread = None
        self._timer = None
        self._guard_paths()
        self._fd = os.open(self._root / 'service.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            s = os.fstat(self._fd)
            if not stat.S_ISREG(s.st_mode) or s.st_mode & 0o077 or s.st_uid != os.getuid():
                raise Reject('unsafe_root')
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            os.close(self._fd)
            self._fd = None
            raise Reject('service_busy') from None
        try:
            if (self._root/'config.json').exists():
                from .supervised import load_config
                self._config=load_config(self._root/'config.json',self._root)
            self._load_observation()
        except Exception:
            os.close(self._fd);self._fd=None
            raise Reject('invalid_persisted_state') from None

    def _load_observation(self):
        import sqlite3,json
        path=self._root/'worker'/'orders.sqlite3'
        if path.exists():
            db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
            try:
                row=db.execute('SELECT data FROM tasks ORDER BY rowid DESC LIMIT 1').fetchone()
                if row:self._state=self._project(json.loads(row[0]),loaded=True)
            finally:db.close()

    @staticmethod
    def _project(s,*,loaded=False):
        import time
        from .domain import number
        result={k:s[k] for k in ('status','phase','intent','primary_filled','hedge_filled','residual_base')}
        result['fees_by_currency']={k:str(number(v)) for k,v in s.get('fees_by_currency',{}).items() if k in ('USD','USDC','USDT')}
        result['fills']={label:[{k:r[k] for k in ('id','quantity','price','fee','fee_currency')} for r in s['fills'][label]] for label in ('primary','hedge')}
        result['journal']=[{k:e[k] for k in ('sequence','event','at')} for e in s['journal']]
        if loaded:
            result['observed_at']=s['journal'][-1]['at'] if s['journal'] else None
            result['loaded_at']=time.time()
        else:result['observed_at']=time.time()
        return result

    @property
    def root(self):
        return self._root

    def _guard_paths(self):
        for p in (self._root, *self._root.parents):
            if p.is_symlink():
                raise Reject('unsafe_root')
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        s = self._root.stat()
        if s.st_mode & 0o077 or s.st_uid != os.getuid():
            raise Reject('unsafe_root')
        for p in self._root.rglob('*'):
            if p.is_symlink():
                raise Reject('unsafe_root')

    def _ensure_open(self):
        if self._closed:
            raise Reject('service_closed')

    def configure(self, config):
        import json
        from .supervised import load_config
        with self._operation:
            self._ensure_open()
            if self._running or (self._root / 'worker' / 'orders.sqlite3').exists():
                raise Reject('configuration_locked')
            if not isinstance(config, dict) or set(config) != {'version','legs','intent','lifetime','poll_seconds'}:
                raise Reject('invalid_config')
            from .supervised import FIELDS, INTENT
            if not isinstance(config['legs'],list) or len(config['legs']) != 2:
                raise Reject('invalid_config')
            for leg in config['legs']:
                if not isinstance(leg,dict) or not isinstance(leg.get('identity'),dict):raise Reject('invalid_config')
                fields=FIELDS | ({'fee_rate_cap'} if leg['identity'].get('deployment')=='extended-mainnet-starknet' else set())
                if set(leg)!=fields or set(leg['identity'])!={'deployment','account','signer','api_key_index'}:raise Reject('invalid_config')
            if not isinstance(config['intent'],dict) or set(config['intent'])!=INTENT:raise Reject('invalid_config')
            path=self._root/'config.pending'
            try:
                fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,'w') as f:
                    json.dump(config,f,allow_nan=False);f.flush();os.fsync(f.fileno())
                parsed=load_config(path,self._root)
                from .keystore import KeyStore
                for n,spec in enumerate(parsed.specs):
                    vault=self._root/f'vault-{n}'
                    if (vault/'key.json').exists() and KeyStore(vault).identity()!=spec.identity:raise Reject('identity_locked')
                os.replace(path,self._root/'config.json')
                with self._meta:self._config=parsed;self._checked=False
            except Exception:raise Reject('invalid_config') from None
            finally:path.unlink(missing_ok=True)
            return self.status()

    def _begin(self):
        if not self._operation.acquire(blocking=False):raise Reject('operation_busy')
        try:
            self._ensure_open();self._guard_paths()
            if self._config is None or self._running:raise Reject('operation_refused')
            return self._epoch
        except BaseException:self._operation.release();raise

    def _fence(self,epoch):
        # Authorization takes Sessions.lock; never call it while holding _meta.
        if hasattr(self,'_authorization') and not self._authorization():raise Reject('authority_revoked')
        with self._meta:self._fence_epoch(epoch)

    def _fence_epoch(self,epoch):
        if self._closed or epoch!=self._epoch:raise Reject('authority_revoked')

    def setup(self,leg,secret,passphrase):
        from .keystore import KeyStore
        from .web_auth import bounded_secret
        epoch=self._begin()
        try:
            if type(leg) is not int or leg not in (0,1):raise Reject('invalid_leg')
            bounded_secret(passphrase,minimum=16)
            spec=self._config.specs[leg]
            if spec.identity.deployment=='extended-mainnet-starknet':
                from .extended_private import ExtendedCredentials
                if not isinstance(secret,dict) or set(secret)!={'api_key','stark_private_key','stark_public_key','vault'}:raise Reject('invalid_credentials')
                import re
                raw_vault=secret['vault']
                if not isinstance(raw_vault,str) or not re.fullmatch(r'0|[1-9][0-9]{0,19}',raw_vault):raise Reject('invalid_credentials')
                vault_id=int(raw_vault)
                if vault_id>=2**64:raise Reject('invalid_credentials')
                secret=ExtendedCredentials(**dict(secret,vault=vault_id))
            elif not isinstance(secret,str) or len(secret)>1024:raise Reject('invalid_credentials')
            self._fence(epoch)
            vault=KeyStore(self._root/f'vault-{leg}')
            try:vault.create(spec.identity,secret,passphrase)
            finally:vault.lock()
            self._checked=False
            return self.status()
        except Exception:raise Reject('setup_refused') from None
        finally:self._operation.release()

    def _open(self,passphrases,epoch,readonly):
        from .supervised import validate_public,open_sessions
        from .web_auth import bounded_secret
        if not isinstance(passphrases,list) or len(passphrases)!=2:raise Reject('two_passphrases_required')
        for p in passphrases:bounded_secret(p,minimum=16)
        validate_public(self._config,reader=self._reader)
        self._fence(epoch)
        sessions=open_sessions(self._config,self._root,session_factory=self._factory)
        try:
            with self._meta:
                self._fence_epoch(epoch);self._sessions=sessions
            for s,p in zip(sessions,passphrases):
                self._fence(epoch)
                if readonly:s.unlock_readonly(p,lifetime=min(120,self._config.lifetime))
                else:s.unlock(p,confirmation='ARM EXACT SCOPED ORDERS',lifetime=self._config.lifetime)
                self._fence(epoch)
        except BaseException:
            self._dispose(sessions)
            with self._meta:
                if self._sessions is sessions:self._sessions=[]
            raise
        return sessions

    @staticmethod
    def _dispose(sessions):
        for s in sessions:
            try:s.close()
            except Exception:pass
            finally:
                try:s.vault.lock()
                except Exception:pass

    def check(self,passphrases):
        import asyncio
        from .domain import number
        epoch=self._begin()
        try:
            sessions=self._open(passphrases,epoch,True)
            async def read():
                rows=[]
                for s in sessions:
                    await s.registration()
                    positions=await s.positions();orders=await s.open_orders()
                    rows.append(dict(registration_valid=True,positions=[dict(symbol=str(p['symbol']),quantity=str(number(p['quantity']))) for p in positions],open_order_count=len(orders)))
                return rows
            rows=asyncio.run(read());self._fence(epoch)
            with self._meta:self._checked=True
            return dict(self.status(),accounts=rows)
        except Exception:
            self._checked=False
            raise Reject('check_refused') from None
        finally:
            self._dispose(self._sessions);self._sessions=[];self._operation.release()

    def start(self,passphrases,confirmation):
        from .signer_session import ACK
        import time
        epoch=self._begin()
        timer=None
        handoff=[]
        try:
            if confirmation!=ACK:raise Reject('fresh_confirmation_required')
            if self._state is not None and self._state['status'] in ('UNKNOWN','ATTENTION','COMPLETED_CONFIRMED'):raise Reject('task_locked')
            self._fence(epoch)
            self._halt=threading.Event();ready=threading.Event()
            timer=threading.Timer(self._config.lifetime,lambda:self.stop(_timer=timer))
            timer.daemon=True
            with self._meta:
                self._fence_epoch(epoch)
                self._timer=timer
                self._running=True;self._enabled=False;self._error=None
            # Linux PDEATHSIG follows the spawning thread, not just its process.
            # Spawn signers in the durable worker, never the short-lived HTTP handler.
            handoff=[passphrases]
            self._thread=threading.Thread(target=self._run,args=(handoff,epoch,ready,timer),daemon=True,name='live-worker')
            self._thread.start()
            with self._meta:
                if self._timer is timer:timer.start()
            if not ready.wait(45):self.stop();raise Reject('start_timeout')
            if self._error:raise Reject('start_refused')
            return self.status()
        except Exception:
            handoff.clear()
            if timer is not None:self.stop(_timer=timer)
            self._dispose(self._sessions)
            raise Reject('start_refused') from None
        finally:self._operation.release()

    def _run(self,handoff,epoch,ready,timer):
        import asyncio,time
        sessions=[]
        from .live_worker import Worker
        from .signer_session import ACK
        async def run():
            for s in sessions:await s.registration()
            self._fence(epoch)
            with Worker(self._root/'worker',*sessions) as worker:
                worker.add(self._config.intent);worker.arm(ACK,lifetime=self._config.lifetime)
                try:
                    while not self._halt.is_set():
                        self._fence(epoch)
                        if not all(s.alive for s in sessions):raise Reject('signer_expired')
                        state=await worker.tick(self._config.intent['id'])
                        with self._meta:self._state=self._project(state)
                        ready.set()
                        if state['status'] in ('UNKNOWN','ATTENTION','COMPLETED_CONFIRMED'):break
                        await asyncio.to_thread(self._halt.wait,self._config.poll_seconds)
                finally:worker.armed=False
        try:
            sessions=self._open(handoff.pop(),epoch,False)
            self._fence(epoch)
            with self._meta:self._enabled=True
            asyncio.run(run())
        except Exception:
            with self._meta:self._error='execution_refused'
        finally:
            self._dispose(sessions)
            timer.cancel()
            with self._meta:
                if self._timer is timer:
                    self._timer=None
                    self._running=False;self._enabled=False
            ready.set()

    def status(self):
        with self._meta:
            return dict(mode='LIVE', configured=self._config is not None,
                        vaults=[dict(configured=(self._root / f'vault-{n}' / 'key.json').is_file()) for n in range(2)],
                        running=self._running, trading_enabled=self._enabled,
                        checked=self._checked, state=self._state, warning=WARNING,
                        last_error=self._error)

    def stop(self,*,_timer=None):
        with self._meta:
            # Timer ownership and revocation are atomic, including queued callbacks.
            if _timer is not None and self._timer is not _timer:return self.status()
            self._epoch += 1
            self._enabled = False
            self._checked = False
            sessions=list(self._sessions)
            timer=self._timer
            halt=getattr(self,'_halt',None)
            if halt:halt.set()
            thread=self._thread
            if thread is not None and thread.ident is None:
                self._running=False
                self._timer=None
                self._thread=None
        if timer:timer.cancel()
        self._dispose(sessions)
        if thread and thread.ident is not None and thread is not threading.current_thread():thread.join(timeout=4)
        return self.status()

    def close(self):
        self._closed=True
        self.stop()
        # Retain application ownership until outstanding KDF/RPC work is fenced.
        def release():
            with self._operation:
                if self._thread and self._thread is not threading.current_thread():self._thread.join()
                with self._meta:
                    if self._fd is not None:os.close(self._fd);self._fd=None
        if self._operation.locked() or (self._thread and self._thread.is_alive()):
            threading.Thread(target=release,daemon=True).start()
        else:release()
