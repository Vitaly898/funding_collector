"""Authenticated directory routes use the normal owner/CSRF boundary."""
from tests_fc.test_live_web_http import server,request,login

def test_markets_and_quote_auth_csrf_and_exact_payload(server):
    server.sessions.ttl=30
    server.service.markets=lambda primary,hedge:dict(pairs=[],primary=primary,hedge=hedge)
    server.service.quote=lambda **kw:dict(legs=[],base=kw['primary_symbol'])
    for path,body in [('/api/live/markets',dict(primary='a',hedge='b')),
                      ('/api/live/quote',dict(primary='a',hedge='b',primary_symbol='ETH',hedge_symbol='ETH',side='buy',offset_pct='1',protection_bps='50'))]:
        assert request(server,path,'POST',body)[0]==401
        cookie,csrf=login(server)
        assert request(server,path,'POST',body,cookie)[0]==403
        assert request(server,path,'POST',body,cookie,csrf)[0]==200
        assert request(server,path,'POST',dict(body,secret='never'),cookie,csrf)[0]==400
