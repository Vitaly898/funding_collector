"""Local supervision contracts; dummy identities and offline transports only."""
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import os
import pty
import select
import time
import tempfile
import unittest


def config():
    from tests_fc.test_live_worker import pair, intent
    p,h=pair()
    from funding_collector.keystore import _binding
    p.identity=_binding(p.identity.deployment,p.identity.account,None,'02'*32)
    h.identity=_binding(h.identity.deployment,h.identity.account,4,'01'*40)
    return dict(version=1,lifetime=30,poll_seconds=0.1,intent=intent(),legs=[
        dict(identity=asdict(v.identity),symbol='ETH',asset_id=v.market.market_id,
             max_quantity='1',max_notional='5000',max_slippage_bps='50') for v in (p,h)])


class CommandTests(unittest.TestCase):
    def invoke(self,*args):
        return subprocess.run([sys.executable,'-m','funding_collector','live',*args],
                              capture_output=True,text=True,timeout=15)

    def test_help_check_status_are_inert_and_config_is_exact(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'absent';p=Path(d)/'public.json';p.write_text(json.dumps(config()))
            r=self.invoke('check','--config',str(p),'--root',str(root))
            self.assertEqual(r.returncode,0,r.stderr)
            row=json.loads(r.stdout);self.assertFalse(row['trading_enabled'])
            self.assertFalse(row['actual_accounts_read']);self.assertFalse(root.exists())
            r=self.invoke('status','--root',str(root))
            self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(json.loads(r.stdout)['tasks'],[])
            self.assertFalse(root.exists())
            c=config();c['secret']='NOT_ALLOWED';p.write_text(json.dumps(c))
            r=self.invoke('check','--config',str(p),'--root',str(root))
            self.assertNotEqual(r.returncode,0);self.assertNotIn('NOT_ALLOWED',r.stdout+r.stderr)
            self.assertEqual(self.invoke('--help').returncode,0)

    def test_public_validation_precedes_any_session_initialization(self):
        from funding_collector import supervised as s
        from unittest.mock import Mock
        from tests_fc.supervised_probe import public_reader
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'config.json';p.write_text(json.dumps(config()))
            c=s.load_config(p,Path(d)/'private')
            sessions=Mock(side_effect=AssertionError('must not initialize'))
            bad=lambda spec: public_reader(spec,asset_offset=1)
            from funding_collector.domain import Reject
            s.validate_public(c,reader=public_reader)
            with self.assertRaises(Reject):s.validate_public(c,reader=bad)
            with self.assertRaises(Reject):
                s.open_sessions(c,Path(d)/'private',session_factory=sessions)
            sessions.assert_not_called()
            self.assertFalse((Path(d)/'private').exists())

    def test_unlock_refuses_nonterminal_and_no_arm_flag_or_secret_arguments(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'public.json';p.write_text(json.dumps(config()))
            r=self.invoke('unlock','--config',str(p),'--root',str(Path(d)/'absent'))
            self.assertNotEqual(r.returncode,0);self.assertFalse((Path(d)/'absent').exists())
            helptext=self.invoke('unlock','--help').stdout
            self.assertNotIn('--password',helptext);self.assertNotIn('--arm',helptext)
            rejected=self.invoke('unlock','--config',str(p),'--password','SECRET_ARGUMENT_SENTINEL')
            self.assertNotEqual(rejected.returncode,0)
            self.assertNotIn('SECRET_ARGUMENT_SENTINEL',rejected.stdout+rejected.stderr)


PASSWORD='offline-only-long-password'
KEYS=('02'*32,'01'*40)


def terminal_run(root, config_path, action, crash='', refuse_arm=False):
    master,slave=pty.openpty()
    args=[sys.executable,'-m','tests_fc.supervised_probe',str(root),str(config_path),action,crash]
    process=subprocess.Popen(args,stdin=slave,stdout=slave,stderr=slave,close_fds=True)
    os.close(slave);output=b'';cursor=0
    def answer(prompt,value):
        nonlocal output,cursor
        deadline=time.monotonic()+20
        while prompt.encode() not in output[cursor:]:
            if time.monotonic()>deadline:raise AssertionError('terminal prompt timeout: '+repr(output))
            ready,_,_=select.select([master],[],[],.1)
            if ready:
                try:output+=os.read(master,65536)
                except OSError:raise AssertionError('terminal exited before prompt: '+repr(output))
        cursor=len(output)
        os.write(master,(value+'\n').encode())
    try:
        if action=='setup':
            for key in KEYS:
                answer('Dedicated execution key:',key)
                answer('New local passphrase:',PASSWORD)
                answer('Repeat passphrase:',PASSWORD)
        elif action=='account-check':
            answer('Read-only local passphrase for primary:',PASSWORD)
            answer('Read-only local passphrase for hedge:',PASSWORD)
        else:
            answer('Type ARM EXACT SCOPED ORDERS:', 'NO' if refuse_arm else 'ARM EXACT SCOPED ORDERS')
            if not refuse_arm:
                answer('Local passphrase for primary:',PASSWORD)
                answer('Local passphrase for hedge:',PASSWORD)
        deadline=time.monotonic()+25
        while process.poll() is None and time.monotonic()<deadline:
            ready,_,_=select.select([master],[],[],.1)
            if ready:
                try:output+=os.read(master,65536)
                except OSError:break
        process.wait(timeout=5)
        while select.select([master],[],[],0)[0]:
            try:output+=os.read(master,65536)
            except OSError:break
        return process.returncode,output.decode(errors='replace')
    finally:
        if process.poll() is None:process.kill();process.wait()
        os.close(master)


class TerminalTests(unittest.TestCase):
    def prepare(self,d):
        root=Path(d)/'private';p=Path(d)/'config.json';p.write_text(json.dumps(config()))
        rc,output=terminal_run(root,p,'setup');self.assertEqual(rc,0,output)
        for secret in (*KEYS,PASSWORD):self.assertNotIn(secret,output)
        return root,p

    def test_masked_account_check_reads_only_and_creates_no_worker(self):
        with tempfile.TemporaryDirectory() as d:
            root,p=self.prepare(d)
            rc,output=terminal_run(root,p,'account-check')
            self.assertEqual(rc,0,output)
            self.assertIn('ACCOUNT_READ_ONLY',output)
            self.assertIn('"trading_enabled": false',output)
            self.assertNotIn('ARM EXACT',output)
            self.assertFalse((root/'worker').exists())
            self.assertEqual(list(root.glob('signer-*/fake-exchange.json')),[])
            for secret in (*KEYS,PASSWORD):self.assertNotIn(secret,output)

    def test_account_check_sanitizes_counts_and_closes_sessions_on_success(self):
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock, patch
        import io
        from funding_collector import supervised as s
        sessions=[]
        for _ in range(2):
            session=Mock()
            session.__enter__=Mock(return_value=session)
            session.__exit__=Mock(side_effect=lambda *args: None)
            session.registration=AsyncMock(return_value=True)
            session.positions=AsyncMock(return_value=[dict(symbol='SECRET_ACCOUNT_SENTINEL',quantity='12345')])
            session.open_orders=AsyncMock(return_value=[dict(account='SECRET_ACCOUNT_SENTINEL')])
            sessions.append(session)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'absent'
            args=SimpleNamespace(root=root,live_action='account-check')
            out=io.StringIO()
            with patch.object(s,'open_sessions',return_value=sessions),patch.object(sys.stdin,'isatty',return_value=True),patch.object(sys.stderr,'isatty',return_value=True),patch('getpass.getpass',return_value=PASSWORD),redirect_stdout(out):
                self.assertEqual(s.interactive(args,SimpleNamespace(lifetime=30)),0)
            row=json.loads(out.getvalue())
            self.assertEqual(row['accounts'],[dict(registration_valid=True,position_count=1,open_order_count=1)]*2)
            self.assertNotIn('SECRET_ACCOUNT_SENTINEL',out.getvalue())
            self.assertFalse(root.exists())
            for session in sessions:
                session.unlock.assert_not_called()
                session.unlock_readonly.assert_called_once_with(PASSWORD,lifetime=30)
                session.__exit__.assert_called_once()

    def test_account_check_closes_all_sessions_after_read_failure(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock, patch
        from funding_collector import supervised as s
        sessions=[]
        for _ in range(2):
            session=Mock()
            session.__enter__=Mock(return_value=session)
            session.__exit__=Mock(return_value=False)
            session.registration=AsyncMock(side_effect=ValueError('read refused'))
            sessions.append(session)
        with tempfile.TemporaryDirectory() as d:
            args=SimpleNamespace(root=Path(d)/'absent',live_action='account-check')
            with patch.object(s,'open_sessions',return_value=sessions),patch.object(sys.stdin,'isatty',return_value=True),patch.object(sys.stderr,'isatty',return_value=True),patch('getpass.getpass',return_value=PASSWORD):
                with self.assertRaises(ValueError):s.interactive(args,SimpleNamespace(lifetime=30))
            for session in sessions:session.__exit__.assert_called_once()

    def test_sigkill_after_external_accept_before_ack_commit_never_duplicates(self):
        import signal
        from funding_collector.supervised import status
        for stage in ('PRIMARY_SENT','HEDGE_SENT'):
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as d:
                root,p=self.prepare(d)
                rc,output=terminal_run(root,p,'unlock',crash=stage)
                self.assertEqual(rc,-signal.SIGKILL,output)
                # External acceptance is durable, but this leg's ACK is not.
                import sqlite3
                db=sqlite3.connect(root/'worker'/'orders.sqlite3')
                try:state=json.loads(db.execute('SELECT data FROM tasks').fetchone()[0])
                finally:db.close()
                label=stage.split('_')[0].lower()
                self.assertEqual(state['phase'],label.upper()+'_PENDING')
                self.assertEqual(state['orders'][label]['status'],'PENDING')
                self.assertNotIn(label+'_ack_not_fill',[e['event'] for e in state['journal']])
                accepted=[len(json.loads(x.read_text())['calls']) for x in root.glob('signer-*/fake-exchange.json')]
                self.assertEqual(sorted(accepted),[1] if label=='primary' else [1,1])
                rc,output=terminal_run(root,p,'unlock')
                self.assertEqual(rc,0,output)
                self.assertEqual(status(root)['tasks'][0]['status'],'COMPLETED_CONFIRMED')
                counts=[len(json.loads(x.read_text())['calls']) for x in root.glob('signer-*/fake-exchange.json')]
                self.assertEqual(sorted(counts),[1,1])

    def test_masked_setup_unlock_runs_real_service_through_fake_sdk(self):
        with tempfile.TemporaryDirectory() as d:
            root,p=self.prepare(d)
            rc,output=terminal_run(root,p,'unlock');self.assertEqual(rc,0,output)
            self.assertIn('completed_confirmed_positions_remain_open',output)
            from funding_collector.supervised import status
            before=status(root);self.assertEqual(before['tasks'][0]['status'],'COMPLETED_CONFIRMED')
            rc,again=terminal_run(root,p,'unlock',refuse_arm=True);self.assertNotEqual(rc,0)
            self.assertEqual(status(root),before)
            for path in root.rglob('*'):
                if path.is_file():
                    data=path.read_bytes()
                    for secret in (*KEYS,PASSWORD):self.assertNotIn(secret.encode(),data)
            for secret in (*KEYS,PASSWORD):self.assertNotIn(secret,output+again)

    def test_real_sigkill_intents_recovery_requires_fresh_arm_no_duplicate(self):
        import signal
        from funding_collector.supervised import status
        for stage in ('PRIMARY_PENDING','HEDGE_PENDING'):
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as d:
                root,p=self.prepare(d)
                rc,output=terminal_run(root,p,'unlock',crash=stage)
                self.assertEqual(rc,-signal.SIGKILL,output)
                before=status(root);self.assertEqual(before['tasks'][0]['phase'],stage)
                rc,_=terminal_run(root,p,'unlock',refuse_arm=True);self.assertNotEqual(rc,0)
                self.assertEqual(status(root),before)
                rc,output=terminal_run(root,p,'unlock');self.assertEqual(rc,0,output)
                self.assertEqual(status(root)['tasks'][0]['status'],'UNKNOWN')
                counts=[len(json.loads(x.read_text())['calls']) for x in root.glob('signer-*/fake-exchange.json')]
                self.assertEqual(sum(counts),0 if stage=='PRIMARY_PENDING' else 1)


class SupervisionSafetyTests(unittest.TestCase):
    def test_expired_unlock_disables_new_submissions_warns_without_flatten(self):
        import asyncio
        from contextlib import redirect_stdout
        import io
        from types import SimpleNamespace
        from funding_collector import supervised as s
        from funding_collector.live_worker import Worker
        from tests_fc.test_live_worker import pair,intent
        async def run(root):
            p,h=pair()
            for v in (p,h):v.alive=False;v.close=lambda: None;v.vault=SimpleNamespace(lock=lambda: None)
            with Worker(root,p,h) as w:
                w.add(intent());w.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
                out=io.StringIO()
                with redirect_stdout(out):await s.supervise(w,SimpleNamespace(lifetime=1,intent=intent(),poll_seconds=.1),(p,h))
                self.assertEqual(p.calls+h.calls,[]);self.assertFalse(w.armed)
                self.assertIn('unlock_expired_submissions_disabled',out.getvalue())
                self.assertIn('"possible_remaining_exposure": true',out.getvalue())
        with tempfile.TemporaryDirectory() as d:asyncio.run(run(d))

    def test_wrong_vault_identity_prevents_all_signer_initializers(self):
        from unittest.mock import Mock
        from funding_collector import supervised as s
        from funding_collector.keystore import KeyStore
        from funding_collector.live import Identity
        from funding_collector.domain import Reject
        with tempfile.TemporaryDirectory() as d:
            root,p=TerminalTests().prepare(d)
            c=s.load_config(p,root)
            from dataclasses import replace
            old=c.specs[1];wrong=Identity(old.identity.deployment,'124',old.identity.signer,4)
            c=replace(c,specs=(c.specs[0],replace(old,identity=wrong)))
            factory=Mock(side_effect=AssertionError('no init'))
            with self.assertRaises(Reject):s.open_sessions(c,root,session_factory=factory)
            factory.assert_not_called()
