"""Offline browser service acceptance; synthetic execution keys only."""
import json
import time
import pytest
from funding_collector.domain import Reject
from funding_collector.live_web_service import LiveService
from tests_fc.test_supervised import config, PASSWORD, KEYS
from tests_fc.supervised_probe import public_reader

def test_configure_persists_and_locks_root(tmp_path):
    root=tmp_path/'private'
    s=LiveService(root)
    try:
        assert s.configure(config())['configured']
        with pytest.raises(Reject): LiveService(root)
        with pytest.raises(Reject): s.configure(dict(config(),secret='not-allowed'))
    finally: s.close()
    s=LiveService(root)
    assert s.status()['configured'] and not s.status()['checked']
    s.close()

class Session:
    def __init__(self,vault,spec):
        from tests_fc.test_live_worker import FakeVenue
        self.__dict__.update(FakeVenue(spec.identity,spec.asset_id).__dict__)
        self.vault=vault;self.alive=False;self.closed=False
    def unlock(self,password,**kwargs):self.alive=True
    def unlock_readonly(self,password,**kwargs):self.alive=True
    def close(self):self.closed=True;self.alive=False;self.authority.lock()
    async def registration(self):return True
    from tests_fc.test_live_worker import FakeVenue
    positions=FakeVenue.positions;open_orders=FakeVenue.open_orders
    submit=FakeVenue.submit;order_status=FakeVenue.order_status;fills=FakeVenue.fills

def wait_for(predicate):
    end=time.monotonic()+5
    while time.monotonic()<end:
        if predicate():return
        time.sleep(.02)
    raise AssertionError('worker timed out')

def test_check_and_real_worker_full_primary_before_one_hedge(tmp_path):
    sessions=[]
    def factory(v,s):
        session=Session(v,s);sessions.append(session);return session
    service=LiveService(tmp_path/'private',session_factory=factory,public_reader=public_reader)
    try:
        service.configure(config())
        for n,key in enumerate(KEYS):service.setup(n,key,PASSWORD)
        row=service.check([PASSWORD]*2)
        assert row['checked'] and len(row['accounts'])==2
        assert all(s.closed for s in sessions)
        assert not (service.root/'worker').exists()
        service.start([PASSWORD]*2,'ARM EXACT SCOPED ORDERS')
        wait_for(lambda:len(sessions[-2].calls)==1)
        p,h=sessions[-2:]
        from decimal import Decimal as D
        p.fraction=D('.5');time.sleep(.25);assert not h.calls
        p.fraction=D(1);p.result='filled'
        wait_for(lambda:len(h.calls)==1)
        h.fraction=D(1);h.result='filled'
        wait_for(lambda:not service.status()['running'])
        row=service.status()
        assert row['state']['status']=='COMPLETED_CONFIRMED'
        assert row['state']['residual_base']=='0.00'
        assert len(p.calls)==len(h.calls)==1
        assert not row['trading_enabled']
    finally:service.close()


def test_farming_config_opt_in_persists_scoped_authority_and_observation_only_restart(tmp_path):
    from tests_fc.test_farming import policy
    c=dict(config(),farming=policy())
    service=LiveService(tmp_path/'private')
    try:
        service.configure(c)
        assert all(spec.allow_close for spec in service._config.specs)
        assert service.status()['farming']['policy']==policy()
    finally:service.close()
    service=LiveService(tmp_path/'private')
    try:
        assert service.status()['farming']['configured']
    finally:service.close()


def test_service_runs_bounded_farming_through_actual_worker(tmp_path,monkeypatch):
    from tests_fc.test_farming import Venue,policy
    from tests_fc.test_live_worker import pair
    from funding_collector.farming import FarmingWorker
    import funding_collector.farming as module
    sessions=[];now=[1000]
    class FarmingSession(Venue):
        def __init__(self,vault,spec):
            super().__init__(pair()[len(sessions)%2]);self.vault=vault;self.alive=False;sessions.append(self)
        def unlock(self,*args,**kwargs):self.alive=True
        def close(self):self.alive=False;self.authority.lock()
        async def registration(self):return True
    monkeypatch.setattr(module,'FarmingWorker',lambda *a,**k:FarmingWorker(*a,clock=lambda:now[0],**k))
    service=LiveService(tmp_path/'private',session_factory=FarmingSession,public_reader=public_reader)
    try:
        service.configure(dict(config(),farming=policy()))
        for n,key in enumerate(KEYS):service.setup(n,key,PASSWORD)
        service.start([PASSWORD]*2,'ARM EXACT SCOPED ORDERS')
        wait_for(lambda:service.status()['state']['phase']=='HOLDING')
        now[0]=1060
        wait_for(lambda:not service.status()['running'])
        row=service.status()['state']
        assert row['status']=='DONE' and row['farming']['cycle']==1
        assert len(sessions[-2].calls)==len(sessions[-1].calls)==2
        assert row['analytics']['primary']['signed_base']=='0.00'
    finally:service.close()
