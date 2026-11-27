"""Local HTTP/browser acceptance, synthetic public metadata; no keys/orders.
Run explicitly with Playwright installed (not an exchange integration test).
"""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch

from funding_collector.live_web import make_server
from funding_collector.web_auth import initialize_owner
from tests_fc.test_live_markets import adapters
from playwright.sync_api import sync_playwright, expect


def main():
    out = Path(os.environ['MARKET_BROWSER_OUT'])
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    errors, requests, checks = [], [], []
    def listings(root, deployment):
        adapter = adapters(root, deployment)
        if deployment == 'hyperliquid-mainnet-native':
            original = adapter.catalog
            def catalog():
                data = original()
                row = data['markets'][0]
                data['markets'] += [dict(row, coin='DOGE', units_verified=False),
                                    dict(row, coin='OLD', is_delisted=True)]
                return data
            adapter.catalog = catalog
        return adapter
    with tempfile.TemporaryDirectory(dir=out) as temp, patch('funding_collector.live_markets.public_adapter', listings):
        root = Path(temp) / 'private'
        initialize_owner(root, 'synthetic-owner-password')
        server = make_server(root, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/opt/hermes/tools/chromium-1208/chrome-linux64/chrome'), headless=True, args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--no-zygote', '--single-process'])
                page = browser.new_page(viewport={'width': 1440, 'height': 1100})
                page.route('**/*', lambda r: r.continue_() if r.request.url.startswith(server.origin + '/') else r.abort())
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.on('request', lambda r: requests.append(r.url))
                page.on('response', lambda r: print('HTTP', r.status, r.url, flush=True))
                page.goto(server.origin)
                expect(page.locator('#login-panel')).to_be_visible()
                assert not any('/api/live/' in u for u in requests)
                page.locator('#login [name=password]').fill('synthetic-owner-password')
                page.locator('#login button').click()
                expect(page.locator('#workspace')).to_be_visible()
                page.locator('#new-strategy').click()
                expect(page.locator('#pair-select-0 option[value=ETH]')).to_have_count(1)
                expect(page.locator('#pair-select-1 option[value=ETH]')).to_have_count(1)
                assert page.locator('[id^=pair-search], #load-markets').count() == 0
                checks.append('initial lists load automatically without search/manual button')
                for symbol, reason in [('DOGE', 'Не поддерживается приложением: инструмент не проверен'), ('OLD', 'Снят биржей с торгов')]:
                    option = page.locator('#pair-select-0 option[value=' + symbol + ']')
                    expect(option).to_be_disabled()
                    expect(option).to_have_text(symbol)
                    expect(option).to_have_attribute('title', reason)
                expect(page.locator('#market-status')).to_contain_text('не поддерживаются приложением')
                checks.append('short disabled symbols and distinct app-limit/delisting reasons; common capability note')
                page.locator('#pair-select-0').select_option('ETH')
                page.locator('#pair-select-1').select_option('ETH')
                page.locator('#quote').click()
                expect(page.locator('#save-config')).to_be_enabled()
                page.locator('#hedge-deployment').select_option('lighter-rh-mainnet')
                expect(page.locator('#save-config')).to_be_disabled()
                expect(page.locator('#pair-select-1')).to_have_value('')
                expect(page.locator('#pair-select-1 option[value=ETH]')).to_have_count(1)
                expect(page.locator('#pair-select-0')).to_have_value('ETH')
                checks.append('venue change clears quote/changed selection, preserves other leg')
                page.locator('#pair-select-1').select_option('ETH')
                page.locator('#quote').click()
                expect(page.locator('#save-config')).to_be_enabled()
                # Primary-leg venue changes use their own list, not the hedge's.
                def entropy_list(route):
                    route.fulfill(status=200, json={'markets': [[{'symbol': 'io:NVDA', 'label': 'io:NVDA', 'base': 'NVDA', 'units': 'base', 'supported': True}], []]})
                page.route('**/api/live/markets', entropy_list)
                page.locator('#primary-deployment').select_option('entropy-mainnet-io')
                expect(page.locator('#pair-select-0 option[value="io:NVDA"]')).to_have_count(1)
                expect(page.locator('#pair-select-0')).to_have_value('')
                expect(page.locator('#pair-select-1')).to_have_value('ETH')
                expect(page.locator('#save-config')).to_be_disabled()
                expect(page.locator('#farming-enabled')).to_be_disabled()
                page.unroute('**/api/live/markets', entropy_list)
                page.locator('#primary-deployment').select_option('hyperliquid-mainnet-native')
                expect(page.locator('#pair-select-0 option[value=ETH]')).to_have_count(1)
                page.locator('#pair-select-0').select_option('ETH')
                checks.append('primary venue independently reloads; clears old market/quote and disables unsupported farming')
                # Error/empty are exceptional retry paths, never the normal load step.
                def unavailable(route):
                    route.fulfill(status=503, json={'error': 'public_unavailable'})
                page.route('**/api/live/markets', unavailable)
                page.locator('#hedge-deployment').select_option('lighter-core-mainnet')
                expect(page.locator('#retry-markets-1')).to_be_visible()
                expect(page.locator('#pair-select-1')).to_be_disabled()
                expect(page.locator('#pair-select-1')).to_have_value('')
                expect(page.locator('#market-state-1')).to_contain_text('Не удалось')
                page.unroute('**/api/live/markets', unavailable)
                page.locator('#retry-markets-1').click()
                expect(page.locator('#pair-select-1')).to_be_enabled()
                expect(page.locator('#retry-markets-1')).to_be_hidden()
                def empty(route):
                    route.fulfill(status=200, json={'markets': [[], []]})
                page.route('**/api/live/markets', empty)
                page.locator('#hedge-deployment').select_option('lighter-rh-mainnet')
                expect(page.locator('#market-state-1')).to_contain_text('пустой список')
                expect(page.locator('#retry-markets-1')).to_be_visible()
                page.unroute('**/api/live/markets', empty)
                page.locator('#retry-markets-1').click()
                expect(page.locator('#pair-select-1')).to_be_enabled()
                checks.append('public failure/empty clear old rows and expose working per-leg retry')
                # Hold one response while a newer exchange finishes loading.
                pending = []
                def delayed(route):
                    pending.append(route)
                page.route('**/api/live/markets', delayed)
                page.locator('#hedge-deployment').select_option('lighter-core-mainnet')
                expect(page.locator('#pair-select-1')).to_be_disabled()
                expect(page.locator('#pair-select-1')).to_contain_text('Загрузка инструментов…')
                page.wait_for_function('document.querySelector("#pair-select-1").disabled')
                assert len(pending) == 1
                page.unroute('**/api/live/markets', delayed)
                page.locator('#hedge-deployment').select_option('lighter-rh-mainnet')
                expect(page.locator('#pair-select-1 option[value=ETH]')).to_have_count(1)
                pending[0].fulfill(status=200, json={'markets': [[], [{'symbol': 'STALE', 'label': 'STALE', 'supported': True}]]})
                # A browser task turn lets the old fetch continuation run.
                page.evaluate('()=>new Promise(resolve=>setTimeout(resolve,50))')
                expect(page.locator('#pair-select-1 option[value=STALE]')).to_have_count(0)
                expect(page.locator('#pair-select-0')).to_have_value('ETH')
                checks.append('delayed old-venue response cannot replace current dropdown or other leg')
                page.locator('#pair-select-1').select_option('ETH')
                page.screenshot(path=str(out / 'desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.locator('#strategy-modal').evaluate('(e)=>e.scrollWidth<=e.clientWidth')
                page.screenshot(path=str(out / 'mobile.png'), full_page=True)
                checks.append('desktop/mobile dropdowns; no modal horizontal overflow')
                assert not any(any('/api/live/' + a in u for a in ('configure', 'setup', 'check', 'start')) for u in requests)
                # A current metadata 401 must close/gate the form; old 401 after
                # relogin must not erase the new authenticated session.
                def expired(route):
                    # Revoke the real fixture session as well: a forged HTTP 401
                    # with a still-valid server cookie is not session expiry.
                    server.sessions.revoke()
                    route.continue_()
                page.route('**/api/live/markets', expired)
                page.locator('#hedge-deployment').select_option('lighter-core-mainnet')
                expect(page.locator('#workspace')).to_be_hidden()
                expect(page.locator('#strategy-modal')).not_to_be_visible()
                page.unroute('**/api/live/markets', expired)
                page.locator('#login [name=password]').fill('synthetic-owner-password')
                page.locator('#login button').click()
                expect(page.locator('#workspace')).to_be_visible()
                page.locator('#new-strategy').click()
                expect(page.locator('#pair-select-0 option[value=ETH]')).to_have_count(1)
                expect(page.locator('#pair-select-1 option[value=ETH]')).to_have_count(1)
                pending.clear()
                page.route('**/api/live/markets', delayed)
                page.locator('#hedge-deployment').select_option('lighter-rh-mainnet')
                expect(page.locator('#pair-select-1')).to_be_disabled()
                assert len(pending) == 1
                page.locator('#close-strategy').click()
                with page.expect_response('**/api/auth/status') as refreshed:
                    page.locator('#logout').click()
                assert refreshed.value.status == 200
                page.evaluate('()=>new Promise(resolve=>setTimeout(resolve,50))')
                expect(page.locator('#workspace')).to_be_hidden()
                page.unroute('**/api/live/markets', delayed)
                page.locator('#login [name=password]').fill('synthetic-owner-password')
                page.locator('#login button').click()
                expect(page.locator('#workspace')).to_be_visible()
                page.locator('#new-strategy').click()
                expect(page.locator('#pair-select-0 option[value=ETH]')).to_have_count(1)
                expect(page.locator('#pair-select-1 option[value=ETH]')).to_have_count(1)
                pending[0].fulfill(status=401, json={'error': 'expired_old_session'})
                page.evaluate('()=>new Promise(resolve=>setTimeout(resolve,50))')
                expect(page.locator('#workspace')).to_be_visible()
                expect(page.locator('#strategy-modal')).to_be_visible()
                checks.append('metadata 401 gates UI; logout/relogin fences late old metadata 401')
                page.locator('#close-strategy').click()
                page.locator('#logout').click()
                expect(page.locator('#workspace')).to_be_hidden()
                assert not errors, errors
                checks.append('no private execution/setup/configure calls; logout clears UI')
                browser.close()
            result = {'checks': checks, 'passed': len(checks), 'elapsed_seconds': round(time.monotonic()-started, 3), 'page_errors': errors}
            (out / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(json.dumps(result, ensure_ascii=False))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == '__main__':
    main()
