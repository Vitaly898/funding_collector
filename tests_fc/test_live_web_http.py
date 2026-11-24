"""Real stdlib HTTP acceptance, no external requests or credentials."""
import http.client
import json
import threading
import time
import pytest
from funding_collector.web_auth import initialize_owner

class Service:
    def __init__(self,root):self.stops=0;self.calls=[]
    def stop(self):self.stops+=1;return self.status()
    def status(self):return {'mode':'LIVE','configured':False,'running':False}
    def configure(self,config):self.calls.append(config);return self.status()
    def close(self):self.stop()

@pytest.fixture
def server(tmp_path):
    from funding_collector.live_web import make_server
    initialize_owner(tmp_path,'synthetic-owner-password')
    s=make_server(tmp_path,host='127.0.0.1',port=0,service_factory=Service,session_ttl=.4)
    t=threading.Thread(target=s.serve_forever,daemon=True);t.start()
    yield s
    s.shutdown();s.server_close();t.join()

def request(s,path,method='GET',body=None,cookie=None,csrf=None,origin=True,headers=None,timeout=3):
    c=http.client.HTTPConnection(*s.server_address,timeout=timeout)
    h={'Host':s.public_host}
    if origin:h['Origin']=s.origin
    if cookie:h['Cookie']=cookie
    if csrf:h['X-CSRF-Token']=csrf
    if body is not None:h['Content-Type']='application/json'
    if headers:h.update(headers)
    c.request(method,path,body=json.dumps(body) if isinstance(body,dict) else body,headers=h)
    r=c.getresponse();data=r.read();out=(r.status,dict(r.getheaders()),json.loads(data) if data.startswith(b'{') else data)
    c.close();return out

def login(s):
    code,h,row=request(s,'/api/auth/status')
    assert code==200 and not row['authenticated']
    challenge=h['Set-Cookie'].split(';')[0]
    code,h,row=request(s,'/api/auth/login','POST',{'password':'synthetic-owner-password'},challenge,row['csrf_token'])
    assert code==200 and row['authenticated']
    return h['Set-Cookie'].split(';')[0],row['csrf_token']

def test_login_private_gate_csrf_logout(server):
    s=server
    assert request(s,'/api/live/status')[0]==401
    assert request(s,'/api/live/setup','POST',body='not json')[0]==401
    cookie,csrf=login(s)
    assert request(s,'/api/live/status',cookie=cookie)[0]==200
    assert request(s,'/api/live/configure','POST',{'config':{}},cookie)[0]==403
    assert request(s,'/api/live/configure','POST',{'config':{}},cookie,csrf)[0]==200
    assert s.service.calls==[{}]
    before=s.service.stops
    code,headers,row=request(s,'/api/auth/logout','POST',{},cookie,csrf)
    assert code==200
    # Server revocation is authoritative; a late response must not clear a newer login cookie.
    assert 'Set-Cookie' not in headers
    assert s.service.stops>before
    assert request(s,'/api/live/status',cookie=cookie)[0]==401

def test_old_logout_cannot_revoke_new_login(server,monkeypatch):
    from funding_collector.live_web import Handler
    server.sessions.ttl=30
    old_cookie,old_csrf=login(server)
    entered=threading.Event();release=threading.Event();result=[]
    original=Handler.body
    def delayed(handler,fields):
        if handler.path=='/api/auth/logout':
            entered.set();assert release.wait(3)
        return original(handler,fields)
    monkeypatch.setattr(Handler,'body',delayed)
    thread=threading.Thread(target=lambda:result.append(request(server,'/api/auth/logout','POST',{},old_cookie,old_csrf)))
    thread.start();assert entered.wait(2)
    new_cookie,new_csrf=login(server)
    release.set();thread.join(3)
    assert result[0][0]==401
    assert request(server,'/api/live/status',cookie=new_cookie)[0]==200


def test_success_response_is_sent_after_kdf_slot_is_released(server,monkeypatch):
    from funding_collector.live_web import Handler
    observed=[];original=Handler.reply
    def inspect(handler,code,data,**kwargs):
        if handler.path in ('/api/auth/login','/api/live/configure') and code==200:
            free=handler.server.kdf.acquire(blocking=False)
            if free:handler.server.kdf.release()
            observed.append(free)
        return original(handler,code,data,**kwargs)
    monkeypatch.setattr(Handler,'reply',inspect)
    cookie,csrf=login(server)
    assert request(server,'/api/live/configure','POST',{'config':{}},cookie,csrf)[0]==200
    assert observed==[True,True]


def test_expiry_actively_fences(server):
    cookie,csrf=login(server)
    before=server.service.stops
    time.sleep(.55)
    assert server.service.stops>before
    assert request(server,'/api/live/status',cookie=cookie)[0]==401

@pytest.mark.parametrize('headers',[{'Host':'evil.test'},{'Origin':'https://evil.test'},{'Transfer-Encoding':'chunked'},{'Content-Length':'17000'}])
def test_boundary_rejects(server,headers):
    assert request(server,'/api/auth/login','POST','{}',headers=headers)[0] in (400,403,413)

def test_duplicate_json_and_generic_errors(server):
    cookie,csrf=login(server)
    assert request(server,'/api/live/configure','POST','{"config":{},"config":{}}',cookie,csrf)[0]==400
    code,h,data=request(server,'/api/live/configure','POST',{'config':{},'secret':'SENTINEL'},cookie,csrf)
    assert code==400 and 'SENTINEL' not in str(data)
    assert h['Cache-Control']=='no-store'
    assert 'frame-ancestors' in h['Content-Security-Policy']

def test_serve_refuses_public_bind_and_invalid_origin(tmp_path):
    from funding_collector.live_web import make_server
    for options in ({'host':'0.0.0.0'},{'origin':'http://example.com'},{'origin':'https://example.com/path'}):
        with pytest.raises((ValueError,OSError)):make_server(tmp_path,**options)
