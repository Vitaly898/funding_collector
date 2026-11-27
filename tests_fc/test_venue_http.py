"""Real authenticated HTTP -> service -> single public adapter, offline."""
import threading
import pytest
from funding_collector.live_web import make_server
from funding_collector.web_auth import initialize_owner
from tests_fc.test_live_web_http import login, request
from tests_fc.test_live_markets import adapters, HL, CORE, RH

@pytest.fixture
def directory_server(tmp_path, monkeypatch):
    calls=[]
    def isolated(root, dep):
        calls.append(dep)
        if dep == CORE:
            raise OSError('PRIVATE_SENTINEL')
        return adapters(root, dep)
    monkeypatch.setattr('funding_collector.live_markets.public_adapter', isolated)
    initialize_owner(tmp_path, 'synthetic-owner-password')
    server=make_server(tmp_path, port=0)
    thread=threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    yield server,calls
    server.shutdown();server.server_close();thread.join(3)

def test_venue_endpoint_is_independent_authenticated_and_scoped(directory_server):
    server,calls=directory_server
    path='/api/live/markets/'+HL
    assert request(server,path)[0] == 401
    assert calls == []
    cookie,csrf=login(server)
    assert request(server,path,cookie=cookie,headers={'Origin':'https://evil.test'})[0] == 403
    for dep in (HL,RH):
        code,headers,row=request(server,'/api/live/markets/'+dep,cookie=cookie)
        assert code == 200
        assert set(row)=={'deployment','markets','observed_at','max_age_seconds'}
        assert row['deployment']==dep and row['markets'][0]['symbol']=='ETH'
        assert headers['Cache-Control']=='no-store'
    assert calls == [HL,RH]
    code,_,row=request(server,'/api/live/markets/'+CORE,cookie=cookie)
    assert (code,row)==(409,{'error':'operation_refused'})
    assert 'PRIVATE_SENTINEL' not in str(row)
    assert request(server,'/api/live/markets/evil',cookie=cookie)[0]==409
    assert calls == [HL,RH,CORE]
    assert request(server,'/api/live/markets/'+HL+'/extra',cookie=cookie)[0]==404
    assert request(server,'/api/live/markets/'+HL,'POST',{},cookie,csrf)[0]==404
    assert request(server,'/api/auth/logout','POST',{},cookie,csrf)[0]==200
    assert request(server,path,cookie=cookie)[0]==401
    assert calls == [HL,RH,CORE]

def test_revoked_inflight_catalogue_error_is_also_fenced(directory_server, monkeypatch):
    server,calls=directory_server
    cookie,_=login(server)
    def revoke(root,dep):
        server.sessions.revoke()
        raise OSError('synthetic public outage')
    monkeypatch.setattr('funding_collector.live_markets.public_adapter',revoke)
    code,_,row=request(server,'/api/live/markets/'+HL,cookie=cookie)
    assert (code,row)==(401,{'error':'authentication_required'})


def test_revoked_inflight_catalogue_is_not_returned(directory_server, monkeypatch):
    server,calls=directory_server
    cookie,_=login(server)
    original=adapters
    def revoke(root,dep):
        server.sessions.revoke()
        return original(root,dep)
    monkeypatch.setattr('funding_collector.live_markets.public_adapter',revoke)
    code,_,row=request(server,'/api/live/markets/'+HL,cookie=cookie)
    assert (code,row)==(401,{'error':'authentication_required'})
