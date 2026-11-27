"""Real localhost browser -> dynamic public adapters -> configure, no keys/orders."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch
from funding_collector.live_web import make_server
from funding_collector.web_auth import initialize_owner
from tests_fc.test_dynamic_native import dynamic_adapters
from tests_fc.test_supervised import config
from playwright.sync_api import sync_playwright, expect


def main():
    out=Path(os.environ['MARKET_BROWSER_OUT']);out.mkdir(parents=True,exist_ok=True)
    started=time.monotonic();requests=[];errors=[]
    with tempfile.TemporaryDirectory(dir=out) as tmp, patch('funding_collector.live_markets.public_adapter',dynamic_adapters):
        root=Path(tmp)/'private';initialize_owner(root,'synthetic-owner-password')
        server=make_server(root,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/opt/hermes/tools/chromium-1208/chrome-linux64/chrome'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--no-zygote','--single-process'])
                page=browser.new_page(viewport={'width':1440,'height':1100})
                page.route('**/*',lambda r:r.continue_() if r.request.url.startswith(server.origin+'/') else r.abort())
                page.on('request',lambda r:requests.append(r.url));page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(server.origin)
                page.locator('#login [name=password]').fill('synthetic-owner-password');page.locator('#login button').click()
                expect(page.locator('#workspace')).to_be_visible()
                page.locator('#tab-connections').click()
                for n,leg in enumerate(config()['legs']):
                    for field in ('account','signer','api_key_index'):
                        value=leg['identity'][field]
                        if value is not None:page.locator(f'#leg-{n} [name={field}]').fill(str(value))
                page.locator('#tab-trade').click();page.locator('#new-strategy').click()
                expect(page.locator('#pair-select-0 option[value=XRP]')).to_have_count(1)
                expect(page.locator('#pair-select-1 option[value=DOGE]')).to_be_enabled()
                page.locator('#pair-select-0').select_option('XRP');page.locator('#pair-select-1').select_option('DOGE')
                page.locator('#config [name=quantity]').fill('10');page.locator('#config [name=label]').fill('dynamic-browser')
                expect(page.locator('#market-status')).to_contain_text('без списка разрешённых тикеров')
                page.locator('#quote').click();expect(page.locator('#save-config')).to_be_enabled()
                assert not any('/api/live/configure' in u for u in requests)
                page.screenshot(path=str(out/'desktop.png'),full_page=True)
                page.set_viewport_size({'width':390,'height':844})
                assert page.locator('#strategy-modal').evaluate('(e)=>e.scrollWidth<=e.clientWidth')
                page.screenshot(path=str(out/'mobile.png'),full_page=True)
                page.set_viewport_size({'width':1440,'height':1100})
                with page.expect_response('**/api/live/configure') as reply:page.locator('#save-config').click()
                assert reply.value.status==200
                data=json.loads(reply.value.request.post_data)['config']
                assert [(leg['symbol'],leg['asset_id']) for leg in data['legs']]==[('XRP',1),('DOGE',77)]
                expect(page.locator('#strategy-list')).to_contain_text('XRP')
                expect(page.locator('#strategy-list')).to_contain_text('DOGE')
                assert not server.service.status()['trading_enabled']
                assert not any(any('/api/live/'+action in u for action in ('setup','check','start')) for u in requests)
                assert not any(root.glob('vault-*/key.json'))
                assert page.evaluate('localStorage.length+sessionStorage.length')==0
                assert not errors,errors
                browser.close()
            result=dict(checks=['automatic dynamic XRP/DOGE dropdowns','real quote and explicit configure HTTP with exact IDs 1/77','desktop/mobile no horizontal overflow','no key setup/account check/execution; authority remains locked'],passed=4,page_errors=errors,elapsed_seconds=round(time.monotonic()-started,3))
            (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
        finally:
            server.shutdown();server.server_close();thread.join(3)


if __name__=='__main__':main()
