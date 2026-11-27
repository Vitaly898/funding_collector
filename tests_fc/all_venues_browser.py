"""Local Auth -> UI -> encrypted custody -> actual spawned SDK adapters -> Worker.
Exchange HTTP is synthetic and all exchange network connections are denied.
"""
import json,os,tempfile,threading,time
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright,expect
from funding_collector.live_web import make_server
from funding_collector.live_web_service import LiveService
from funding_collector.signer_session import SignerSession
from funding_collector.keystore import _binding
from funding_collector.web_auth import initialize_owner
from tests_fc.all_venues_probe import public,factory,ENT,ASTER,EXT,HL,KEYS
from tests_fc.test_extended_integration import bundle,identity

PASSWORD='synthetic-password-long-enough'

def reader(spec):
    from funding_collector.signer_factory import aster_snapshot,extended_snapshot
    adapter=public(spec.artifacts,spec.identity.deployment)
    if spec.identity.deployment==ASTER:return aster_snapshot(adapter,spec)
    if spec.identity.deployment==EXT:return extended_snapshot(adapter,spec)
    return adapter.snapshot(spec.symbol)


def main():
 out=Path(os.environ['MARKET_BROWSER_OUT']);out.mkdir(parents=True,exist_ok=True);started=time.monotonic();results=[]
 for pair,symbols in [((ENT,ASTER),('io:OAI','DOGEUSDT')),((HL,EXT),('XRP','XRP-USD'))]:
  with tempfile.TemporaryDirectory(dir=out) as temp,patch('funding_collector.live_markets.public_adapter',public):
   root=Path(temp)/'private';initialize_owner(root,'synthetic-owner-password')
   server=make_server(root,port=0,service_factory=lambda r:LiveService(r,public_reader=reader,session_factory=lambda v,s:SignerSession(v,s,factory=factory)))
   thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();errors=[]
   try:
    with sync_playwright() as p:
     browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/opt/hermes/tools/chromium-1208/chrome-linux64/chrome'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--no-zygote','--single-process'])
     page=browser.new_page(viewport={'width':1440,'height':1100});page.on('pageerror',lambda e:errors.append(str(e)))
     page.route('**/*',lambda r:r.continue_() if r.request.url.startswith(server.origin+'/') else r.abort())
     page.goto(server.origin);page.locator('#login [name=password]').fill('synthetic-owner-password');page.locator('#login button').click()
     expect(page.locator('#workspace')).to_be_visible();page.locator('#new-strategy').click()
     if pair[0]==ENT:
      # Every supported deployment loads automatically, without reading the
      # companion venue or requiring a load/search action.
      expect(page.locator('#pair-select-0 option[value="XRP"]')).to_be_enabled()
      expect(page.locator('#pair-select-1 option[value="DOGE"]')).to_be_enabled()
      directory_requests=[]
      page.on('request',lambda r:directory_requests.append(r.url) if '/api/live/markets/' in r.url else None)
      for dep,symbol in [('lighter-rh-mainnet','DOGE'),(ENT,'io:OAI'),(ASTER,'DOGEUSDT'),(EXT,'XRP-USD'),(HL,'XRP'),('lighter-core-mainnet','DOGE')]:
       before=len(directory_requests)
       page.locator('#primary-deployment').select_option(dep)
       expect(page.locator(f'#pair-select-0 option[value="{symbol}"]')).to_be_enabled()
       assert directory_requests[before:]==[server.origin+'/api/live/markets/'+dep],directory_requests[before:]
       page.screenshot(path=str(out/(dep+'-catalog.png')),full_page=True)
     for n,(dep,symbol) in enumerate(zip(pair,symbols)):
      page.locator('#'+('hedge' if n else 'primary')+'-deployment').select_option(dep)
      expect(page.locator(f'#pair-select-{n} option[value="{symbol}"]')).to_be_enabled()
     expect(page.locator('#farming-enabled')).to_be_disabled()
     page.locator('#close-strategy').click();page.locator('#tab-connections').click()
     for n,dep in enumerate(pair):
      ident=identity() if dep==EXT else _binding(dep,'0x'+'11'*20,None,KEYS[dep])
      for field in ('account','signer'):page.locator(f'#leg-{n} [name={field}]').fill(getattr(ident,field))
      if dep==EXT:page.locator(f'#leg-{n} [name=fee_rate_cap]').fill('0.0005')
     page.locator('#tab-trade').click();page.locator('#new-strategy').click()
     for n,symbol in enumerate(symbols):
      expect(page.locator(f'#pair-select-{n} option[value="{symbol}"]')).to_be_enabled();page.locator(f'#pair-select-{n}').select_option(symbol)
     page.locator('#config [name=quantity]').fill('.02');page.locator('#config [name=offset_pct]').fill('0')
     page.locator('#quote').click();expect(page.locator('#save-config')).to_be_enabled()
     page.set_viewport_size({'width':390,'height':844});assert page.locator('#strategy-modal').evaluate('(e)=>e.scrollWidth<=e.clientWidth')
     page.screenshot(path=str(out/(pair[1]+'-mobile.png')),full_page=True);page.set_viewport_size({'width':1440,'height':1100})
     with page.expect_response('**/api/live/configure') as response:page.locator('#save-config').click()
     assert response.value.status==200
     data=json.loads(response.value.request.post_data)['config']
     if pair[1]==EXT:assert data['legs'][1]['asset_id']=='9007199254740993' and data['legs'][1]['fee_rate_cap']=='0.0005'
     else:assert type(data['legs'][1]['asset_id']) is str
     page.locator('#tab-connections').click()
     for n,dep in enumerate(pair):
      form=page.locator(f'#setup-{n}')
      if dep==EXT:
       b=bundle()
       for name in ('api_key','stark_private_key','stark_public_key','vault'):form.locator('[name='+name+']').fill(str(getattr(b,name)))
      else:form.locator('[name=secret]').fill(KEYS[dep])
      form.locator('[name=passphrase]').fill(PASSWORD)
      with page.expect_response('**/api/live/setup') as response:form.locator('button').click()
      assert response.value.status==200
     assert len(list(root.glob('vault-*/key.json')))==2
     page.locator('#tab-trade').click();page.locator('#strategy-list button').click()
     for field in ('primary_passphrase','hedge_passphrase'):page.locator('#check [name='+field+']').fill(PASSWORD)
     with page.expect_response('**/api/live/check',timeout=45000) as response:page.locator('#check button').click()
     assert response.value.status==200,response.value.json()
     assert server.service.status()['checked'] and not server.service.status()['state']
     page.locator('#arming > details > summary').click()
     for field in ('primary_passphrase','hedge_passphrase'):page.locator('#start [name='+field+']').fill(PASSWORD)
     page.locator('#start [name=confirmation]').fill('ARM EXACT SCOPED ORDERS')
     with page.expect_response('**/api/live/start',timeout=45000) as response:page.locator('#start button').click()
     assert response.value.status==200,response.value.json()
     deadline=time.monotonic()+35
     while time.monotonic()<deadline:
      state=server.service.status()['state']
      if state and state['status'] in ('COMPLETED_CONFIRMED','ATTENTION','UNKNOWN'):break
      time.sleep(.1)
     assert state and state['status']=='COMPLETED_CONFIRMED',state
     assert state['primary_filled']==state['hedge_filled']=='0.02' and state['residual_base']=='0.00'
     # Public status deliberately omits private order wires. Verify the durable
     # Worker journal instead of weakening that projection boundary.
     import sqlite3
     db=sqlite3.connect(root/'worker'/'orders.sqlite3')
     try:
      durable=json.loads(db.execute('SELECT data FROM tasks WHERE id=?',(data['intent']['id'],)).fetchone()[0])
     finally:db.close()
     assert len(durable['orders'])==2 and durable['status']=='COMPLETED_CONFIRMED'
     assert state['fees_by_currency']==({'USDC':'0.01','USDT':'0.01'} if pair[1]==ASTER else {'USDC':'0.01','USD':'0.009'})
     expect(page.locator('#ledger')).to_contain_text('Исполнено',timeout=10000)
     page.screenshot(path=str(out/(pair[1]+'-completed.png')),full_page=True)
     before=json.dumps(state,sort_keys=True);time.sleep(.3);assert json.dumps(server.service.status()['state'],sort_keys=True)==before
     assert page.evaluate('localStorage.length+sessionStorage.length')==0
     page.locator('#logout').click();expect(page.locator('#login-panel')).to_be_visible()
     assert not server.service.status()['trading_enabled'] and not errors
     browser.close()
    results.append(dict(pair=pair,symbols=symbols,status='COMPLETED_CONFIRMED',actual_sdk_signatures=True,encrypted_custody=True,spawned_signers=True,no_exchange_network=True))
   finally:server.shutdown();server.server_close();thread.join(3)
 record=dict(mode='REAL_LOCAL_AUTH_KEYSTORE_SPAWNED_REAL_SDK_ADAPTERS_WORKER_SYNTHETIC_HTTP',results=results,elapsed_seconds=round(time.monotonic()-started,2))
 (out/'results.json').write_text(json.dumps(record,indent=2));print(json.dumps(record))

if __name__=='__main__':main()
