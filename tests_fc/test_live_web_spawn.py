"""Real HTTP -> encrypted custody -> spawned signer -> Worker; offline venue only."""
import threading
import time
from funding_collector.live_web import make_server
from funding_collector.live_web_service import LiveService
from funding_collector.signer_session import SignerSession
from funding_collector.web_auth import initialize_owner
from tests_fc.supervised_probe import public_reader,offline_factory
from tests_fc.test_supervised import config,KEYS,PASSWORD
from tests_fc.test_live_web_http import request as _request,login

def request(*args,**kwargs):
    return _request(*args,timeout=40,**kwargs)


def test_http_to_actual_spawned_signers_and_worker_then_locked_restart(tmp_path):
    root=tmp_path/'private'
    initialize_owner(root,'synthetic-owner-password')
    def factory(vault,spec):return SignerSession(vault,spec,factory=offline_factory)
    def service(path):return LiveService(path,session_factory=factory,public_reader=public_reader)
    server=make_server(root,port=0,service_factory=service)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        cookie,csrf=login(server)
        assert request(server,'/api/live/configure','POST',{'config':config()},cookie,csrf)[0]==200
        for n,key in enumerate(KEYS):
            assert request(server,'/api/live/setup','POST',{'leg':n,'secret':key,'passphrase':PASSWORD},cookie,csrf)[0]==200
        code,_,row=request(server,'/api/live/check','POST',{'passphrases':[PASSWORD]*2},cookie,csrf)
        assert code==200 and row['checked']
        assert not (root/'worker').exists()
        assert not server.service.status()['trading_enabled']
        code,_,row=request(server,'/api/live/start','POST',{'passphrases':[PASSWORD]*2,'confirmation':'ARM EXACT SCOPED ORDERS'},cookie,csrf)
        assert code==200
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            code,_,row=request(server,'/api/live/status',cookie=cookie)
            assert code==200
            if not row['running']:break
            time.sleep(.05)
        assert row['state']['status']=='COMPLETED_CONFIRMED'
        assert row['state']['residual_base']=='0.00'
        assert not row['trading_enabled']
        assert request(server,'/api/auth/logout','POST',{},cookie,csrf)[0]==200
    finally:
        server.shutdown();server.server_close();thread.join(3)
    # Restart may show old observations, but never reopens a signer or execution authority.
    restarted=service(root)
    try:
        row=restarted.status()
        assert row['configured'] and not row['running'] and not row['trading_enabled'] and not row['checked']
        assert row['state']['status']=='COMPLETED_CONFIRMED'
        import pytest
        from funding_collector.domain import Reject
        with pytest.raises(Reject):restarted.start([PASSWORD]*2,'ARM EXACT SCOPED ORDERS')
    finally:restarted.close()
