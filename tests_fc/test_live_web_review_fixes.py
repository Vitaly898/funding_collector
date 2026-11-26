"""Review regressions: actual lifecycle boundaries, no KDF, SDK or network."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from funding_collector.domain import Reject
from funding_collector.live_web_service import LiveService
from funding_collector.web_auth import Sessions


def offline_config(**overrides):
    """Lifecycle doubles retain the public strategy projection contract."""
    values = dict(lifetime=30, poll_seconds=.01,
                  intent={'id':'offline', 'side':'buy', 'quantity':'0.02'},
                  specs=[SimpleNamespace(identity=SimpleNamespace(deployment=d), symbol='ETH')
                         for d in ('hyperliquid-mainnet-native', 'lighter-core-mainnet')])
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize('expiry', [False, True], ids=['logout', 'expiry'])
def test_open_does_not_invert_session_and_metadata_locks(tmp_path, monkeypatch, expiry):
    from funding_collector import supervised
    service = LiveService(tmp_path/'private')
    service._config = offline_config()
    auth = Sessions(service.stop)
    token, _ = auth.issue()
    epoch = service._epoch
    created = threading.Event()
    authorizing = threading.Event()
    revoking = threading.Event()
    legs = [SimpleNamespace(vault=SimpleNamespace(lock=Mock()), close=Mock(),
                            unlock_readonly=Mock()) for _ in range(2)]
    monkeypatch.setattr(supervised, 'validate_public', lambda *a, **k: None)
    def open_sessions(*a, **k):
        created.set()
        assert revoking.wait(1)
        return legs
    monkeypatch.setattr(supervised, 'open_sessions', open_sessions)
    def authorized():
        if created.is_set():
            authorizing.set()
            assert revoking.wait(1)
        return auth.validate(token) is not None
    service._authorization = authorized
    failures = []
    def opening():
        try:
            service._open(['offline-passphrase']*2, epoch, True)
        except Reject:
            failures.append('revoked')
    def revoking_session():
        assert created.wait(1)
        with auth.lock:
            revoking.set()
            assert authorizing.wait(1)
            if expiry:
                auth._expire(auth.generation)
            else:
                auth.revoke()
    revoke = threading.Thread(target=revoking_session, daemon=True)
    opened = threading.Thread(target=opening, daemon=True)
    revoke.start();opened.start()
    opened.join(1.5);revoke.join(1.5)
    assert not opened.is_alive() and not revoke.is_alive(), 'authorization/stop lock inversion'
    assert failures == ['revoked']
    for leg in legs:
        leg.unlock_readonly.assert_not_called()
        leg.close.assert_called()
        leg.vault.lock.assert_called()
    service.close()


def test_stop_during_session_creation_disposes_unregistered_legs(tmp_path, monkeypatch):
    from funding_collector import supervised
    service = LiveService(tmp_path/'private')
    service._config = offline_config()
    epoch = service._epoch
    legs = [SimpleNamespace(vault=SimpleNamespace(lock=Mock()), close=Mock(),
                            unlock_readonly=Mock()) for _ in range(2)]
    monkeypatch.setattr(supervised, 'validate_public', lambda *a, **k: None)
    def open_sessions(*a, **k):
        service.stop()
        return legs
    monkeypatch.setattr(supervised, 'open_sessions', open_sessions)
    try:
        with pytest.raises(Reject):
            service._open(['offline-passphrase']*2, epoch, True)
        for leg in legs:
            leg.close.assert_called_once()
            leg.vault.lock.assert_called_once()
            leg.unlock_readonly.assert_not_called()
    finally:
        service.close()


def test_failed_close_still_disposes_both_legs_and_releases_operation(tmp_path, monkeypatch):
    service = LiveService(tmp_path/'private')
    service._config = offline_config()
    legs = [SimpleNamespace(vault=SimpleNamespace(lock=Mock()), close=Mock(side_effect=RuntimeError()))
            for _ in range(2)]
    def fail_open(*a):
        service._sessions = legs
        raise Reject('revoked')
    monkeypatch.setattr(service, '_open', fail_open)
    with pytest.raises(Reject):
        service.check(['offline-passphrase']*2)
    assert not service._operation.locked()
    for leg in legs:
        leg.close.assert_called_once()
        leg.vault.lock.assert_called_once()
    service.close()


@pytest.fixture
def timer_lifecycle(tmp_path, monkeypatch):
    from funding_collector import live_worker, supervised
    service = LiveService(tmp_path/'private')
    service._config = offline_config()
    timers = []
    legs = [SimpleNamespace(vault=SimpleNamespace(lock=Mock()), close=Mock(),
                            unlock=Mock(), alive=True) for _ in range(2)]
    for leg in legs:
        async def registration():
            return True
        leg.registration = registration
    class Timer:
        def __init__(self, interval, callback):
            self.callback = callback
            self.active = False
            timers.append(self)
        def start(self):
            self.active = True
        def cancel(self):
            self.active = False
        def fire(self):
            # A callback already queued may run even after cancel().
            self.callback()
    class Worker:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def add(self, *args): pass
        def arm(self, *args, **kwargs): pass
        async def tick(self, *args):
            return dict(status='RUNNING', phase='PRIMARY', intent={'id': 'offline', 'side':'buy', 'quantity':'0.02'},
                        primary_filled='0', hedge_filled='0', residual_base='0',
                        fills={'primary': [], 'hedge': []}, journal=[])
    monkeypatch.setattr(threading, 'Timer', Timer)
    monkeypatch.setattr(live_worker, 'Worker', Worker)
    monkeypatch.setattr(supervised, 'validate_public', lambda *a, **k: None)
    monkeypatch.setattr(supervised, 'open_sessions', lambda *a, **k: legs)
    try:
        yield service, timers, legs
    finally:
        service.close()


def test_failed_start_timer_cannot_revoke_retry(timer_lifecycle, monkeypatch):
    from funding_collector import supervised
    service, timers, legs = timer_lifecycle
    real_thread = threading.Thread
    first_start = True
    class FastFailureThread(real_thread):
        def start(self):
            nonlocal first_start
            failed_run = first_start
            first_start = False
            super().start()
            if failed_run:
                self.join(1)
                assert not self.is_alive(), 'failed worker did not finish before start returned'
    monkeypatch.setattr(threading, 'Thread', FastFailureThread)
    def fail_open(*a, **k):
        raise Reject('offline_failure')
    monkeypatch.setattr(supervised, 'open_sessions', fail_open)
    with pytest.raises(Reject):
        service.start(['offline-passphrase']*2, 'ARM EXACT SCOPED ORDERS')
    failed_timer = timers[0]
    failed_timer_active = failed_timer.active
    monkeypatch.setattr(supervised, 'open_sessions', lambda *a, **k: legs)
    assert service.start(['offline-passphrase']*2, 'ARM EXACT SCOPED ORDERS')['trading_enabled']
    retry_timer = timers[1]
    epoch = service._epoch
    failed_timer.fire()
    assert service._epoch == epoch, 'stale callback revoked the retry epoch'
    assert service.status()['trading_enabled']
    assert retry_timer.active
    for leg in legs:
        leg.close.assert_not_called()
    assert not failed_timer_active, 'failed start left an active timer'
    retry_timer.fire()
    assert service._epoch == epoch + 1
    assert not service.status()['running']
    assert not retry_timer.active
    for leg in legs:
        leg.close.assert_called()


@pytest.mark.parametrize('failure', ['thread_start', 'timer_start'])
def test_start_failure_cleans_timer_and_running_state(timer_lifecycle, monkeypatch, failure):
    service, timers, legs = timer_lifecycle
    if failure == 'thread_start':
        real_thread = threading.Thread
        class BrokenThread(real_thread):
            def start(self):
                raise RuntimeError('offline_thread_start_failure')
        monkeypatch.setattr(threading, 'Thread', BrokenThread)
    else:
        timer_class = threading.Timer
        def fail_start(timer):
            timer.active = True
            raise RuntimeError('offline_timer_start_failure')
        monkeypatch.setattr(timer_class, 'start', fail_start)
    with pytest.raises(Reject):
        service.start(['offline-passphrase']*2, 'ARM EXACT SCOPED ORDERS')
    assert not service.status()['running']
    assert not service.status()['trading_enabled']
    assert not service._operation.locked()
    assert len(timers) == 1 and not timers[0].active
    epoch = service._epoch
    timers[0].fire()
    assert service._epoch == epoch
    if failure == 'thread_start':
        for leg in legs:
            leg.unlock.assert_not_called()
    service.close()


@pytest.fixture
def extended_key_derivation(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'fast_stark_crypto', SimpleNamespace(get_public_key=lambda key: 2))


@pytest.mark.parametrize('vault', ['0', '9007199254740993', '18446744073709551615'])
def test_extended_setup_decodes_exact_decimal_vault(tmp_path, monkeypatch, vault, extended_key_derivation):
    from funding_collector.keystore import KeyStore
    from funding_collector.extended_private import ExtendedCredentials
    create = Mock()
    monkeypatch.setattr(KeyStore, 'create', create)
    service = LiveService(tmp_path/'private')
    identity = SimpleNamespace(deployment='extended-mainnet-starknet')
    service._config = offline_config(specs=[SimpleNamespace(identity=identity, symbol='ETH')])
    secret = dict(api_key='synthetic', stark_private_key='0x1', stark_public_key='0x2', vault=vault)
    try:
        service.setup(0, secret, 'offline-passphrase')
        credentials = create.call_args.args[1]
        assert isinstance(credentials, ExtendedCredentials)
        assert type(credentials.vault) is int and credentials.vault == int(vault)
    finally:
        service.close()


@pytest.mark.parametrize('vault', [True, False, 9007199254740993, 1.0, '', '01', '+1', '-1',
                                  ' 1', '1 ', '1e3', '١', '18446744073709551616'])
def test_extended_setup_rejects_invalid_vault_before_writing(tmp_path, monkeypatch, vault, extended_key_derivation):
    from funding_collector.keystore import KeyStore
    create = Mock()
    monkeypatch.setattr(KeyStore, 'create', create)
    service = LiveService(tmp_path/'private')
    identity = SimpleNamespace(deployment='extended-mainnet-starknet')
    service._config = offline_config(specs=[SimpleNamespace(identity=identity, symbol='ETH')])
    secret = dict(api_key='synthetic', stark_private_key='0x1', stark_public_key='0x2', vault=vault)
    try:
        with pytest.raises(Reject):
            service.setup(0, secret, 'offline-passphrase')
        create.assert_not_called()
        assert not (service.root/'vault-0'/'key.json').exists()
    finally:
        service.close()


@pytest.mark.parametrize('journal, expected', [([dict(sequence=1,event='created',at=1000)],1000), ([],None)])
def test_restart_preserves_durable_observation_time(tmp_path, monkeypatch, journal, expected):
    import json
    import sqlite3
    import time
    root = tmp_path/'private'
    service = LiveService(root)
    service.close()
    (root/'worker').mkdir()
    state = dict(status='UNKNOWN', phase='PRIMARY', intent={'id':'offline', 'side':'buy', 'quantity':'0.02'},
                 primary_filled='0', hedge_filled='0', residual_base='0',
                 fills={'primary':[], 'hedge':[]}, journal=journal)
    with sqlite3.connect(root/'worker'/'orders.sqlite3') as db:
        db.execute('CREATE TABLE tasks (data TEXT)')
        db.execute('INSERT INTO tasks VALUES (?)', (json.dumps(state),))
    monkeypatch.setattr(time, 'time', lambda: 9000)
    restarted = LiveService(root)
    try:
        row = restarted.status()
        assert row['state']['observed_at'] == expected
        assert row['state']['loaded_at'] == 9000
        assert not row['checked'] and not row['trading_enabled']
    finally:
        restarted.close()
