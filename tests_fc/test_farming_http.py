"""Actual HTTP/Auth/encrypted KeyStore/spawned typed signer/Worker acceptance.
Only authoritative venues are synthetic; no external transport permitted.
"""
import json
import threading
import time
import pytest
from funding_collector.live_web import make_server
from funding_collector.live_web_service import LiveService
from funding_collector.signer_session import SignerSession
from funding_collector.web_auth import initialize_owner
from tests_fc.supervised_probe import public_reader
from tests_fc.farming_probe import offline_factory
from tests_fc.test_supervised import config,KEYS,PASSWORD
from tests_fc.test_farming import policy
from tests_fc.test_live_web_spawn import request
from tests_fc.test_live_web_http import login


@pytest.mark.parametrize('action',['lock','logout','expiry'])
def test_farming_holding_revocation_never_closes_or_rearms(tmp_path,action):
    root=tmp_path/'private';initialize_owner(root,'synthetic-owner-password')
    def factory(v,s):return SignerSession(v,s,factory=offline_factory)
    def service(path):return LiveService(path,session_factory=factory,public_reader=public_reader)
    server=make_server(root,port=0,service_factory=service)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        cookie,csrf=login(server)
        c=dict(config(),lifetime=6 if action=='expiry' else 30,farming=policy())
        assert request(server,'/api/live/configure','POST',{'config':c},cookie,csrf)[0]==200
        for n,key in enumerate(KEYS):
            assert request(server,'/api/live/setup','POST',{'leg':n,'secret':key,'passphrase':PASSWORD},cookie,csrf)[0]==200
        assert request(server,'/api/live/start','POST',{'passphrases':[PASSWORD]*2,'confirmation':'ARM EXACT SCOPED ORDERS'},cookie,csrf)[0]==200
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            row=server.service.status()
            if row['state'] and row['state']['phase']=='HOLDING':break
            time.sleep(.05)
        assert row['state']['phase']=='HOLDING'
        if action!='expiry':
            route='/api/live/lock' if action=='lock' else '/api/auth/logout'
            assert request(server,route,'POST',{},cookie,csrf)[0]==200
        deadline=time.monotonic()+10
        while server.service.status()['running'] and time.monotonic()<deadline:time.sleep(.05)
        row=server.service.status()
        assert not row['running'] and not row['trading_enabled']
        assert row['state']['phase']=='HOLDING' and row['state']['status']!='DONE'
        for n in range(2):
            orders=json.loads((root/f'signer-{n}'/'fake-exchange.json').read_text())
            assert len(orders)==1 and orders[0]['reduce_only'] is False
        if action=='logout':assert request(server,'/api/live/status',cookie=cookie)[0]==401
    finally:server.shutdown();server.server_close();thread.join(3)
    restarted=service(root)
    try:
        row=restarted.status()
        assert row['state']['phase']=='HOLDING' and not row['trading_enabled'] and not row['running']
    finally:restarted.close()


def test_farming_http_spawn_close_and_locked_restart(tmp_path,monkeypatch):
    from funding_collector.farming import FarmingWorker
    import funding_collector.farming as farming
    offset=[0]
    monkeypatch.setattr(farming,'FarmingWorker',lambda *a,**kw:FarmingWorker(*a,clock=lambda:time.time()+offset[0],**kw))
    root=tmp_path/'private';initialize_owner(root,'synthetic-owner-password')
    def factory(v,s):return SignerSession(v,s,factory=offline_factory)
    def service(path):return LiveService(path,session_factory=factory,public_reader=public_reader)
    server=make_server(root,port=0,service_factory=service)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        cookie,csrf=login(server)
        assert request(server,'/api/live/configure','POST',{'config':dict(config(),farming=policy())},cookie,csrf)[0]==200
        for n,key in enumerate(KEYS):
            assert request(server,'/api/live/setup','POST',{'leg':n,'secret':key,'passphrase':PASSWORD},cookie,csrf)[0]==200
        assert request(server,'/api/live/start','POST',{'passphrases':[PASSWORD]*2,'confirmation':'ARM EXACT SCOPED ORDERS'},cookie,csrf)[0]==200
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            code,_,row=request(server,'/api/live/status',cookie=cookie)
            assert code==200
            if row['state']['phase']=='HOLDING':offset[0]=61
            if not row['running']:break
            time.sleep(.05)
        assert row['state']['status']=='DONE',row
        assert row['state']['residual_base']=='0.00'
        assert row['state']['farming']['cycle']==1
        for n in range(2):
            orders=json.loads((root/f'signer-{n}'/'fake-exchange.json').read_text())
            assert len(orders)==2 and orders[1]['reduce_only'] is True
        assert request(server,'/api/auth/logout','POST',{},cookie,csrf)[0]==200
        assert request(server,'/api/live/status',cookie=cookie)[0]==401
    finally:server.shutdown();server.server_close();thread.join(3)
    with_state=service(root)
    try:
        row=with_state.status()
        assert row['state']['status']=='DONE' and not row['running'] and not row['trading_enabled']
    finally:with_state.close()
