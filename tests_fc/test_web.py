"""Loopback UI tests. All market fixtures synthetic; no exchange requests."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest


class WebHTTP(unittest.TestCase):
    def setUp(self):
        from funding_collector.web import make_server
        self.tmp = tempfile.TemporaryDirectory()
        self.server = make_server(self.tmp.name, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = '127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        conn.request(method, path, body, headers or {})
        response = conn.getresponse()
        data = response.read()
        result = response.status, dict(response.getheaders()), data
        conn.close()
        return result

    def test_private_foundation_status_is_not_wallet_or_background_readiness(self):
        status, _, body = self.request('GET', '/api/live-foundation-status')
        self.assertEqual(status, 200, body)
        row = json.loads(body)
        self.assertFalse(row['trading_enabled'])
        self.assertFalse(row['wallet_ready'])
        self.assertEqual(row['background_worker'], 'NOT_STARTED')
        self.assertEqual(row['mode'], 'PRIVATE_FOUNDATION_ONLY')
        self.assertEqual(set(row['sdk_bridges']), {'hyperliquid-mainnet-native','lighter-core-mainnet'})
        self.assertFalse(row['credentials_loaded'])
        self.assertEqual(self.post('/api/arm', {})[0], 404)

    def test_live_projection_is_opt_in_readonly_sanitized_and_no_actions(self):
        status,_,body=self.request('GET','/api/live-worker-status')
        self.assertEqual(status,200,body)
        self.assertEqual(json.loads(body)['projection'],'NOT_CONFIGURED')
        from tests_fc.test_live_worker import pair,intent
        from funding_collector.live_worker import Worker
        root=Path(self.tmp.name)/'separate-live'
        p,h=pair()
        with Worker(root/'worker',p,h) as w:
            w.add(intent())
            state=w.state('bounded-test');state['reason']='SECRET_AUTH_SENTINEL'
            w._save(state)
            self.server.live_root=root
            status,_,body=self.request('GET','/api/live-worker-status')
            self.assertEqual(status,200,body)
            row=json.loads(body);self.assertTrue(row['worker_process_active'])
            self.assertEqual(row['tasks'][0]['status'],'NEW')
            for forbidden in ('SECRET_AUTH_SENTINEL',p.identity.signer,p.identity.account,'bounded-test','client_id'):
                self.assertNotIn(forbidden,body.decode())
            for route in ('/api/live-worker-status','/api/unlock','/api/live/arm'):
                self.assertEqual(self.post(route,{})[0],404)
        row=json.loads(self.request('GET','/api/live-worker-status')[2])
        self.assertFalse(row['worker_process_active'])

    def test_entropy_catalog_explicit_public_read_and_ui_preset(self):
        from unittest.mock import patch
        with patch('funding_collector.entropy.EntropyPublicAdapter.catalog', return_value={
                'own_dex':'io','total':1,'markets':[], 'trading_enabled':False}) as catalog:
            self.assertEqual(self.request('GET','/')[0],200)
            catalog.assert_not_called()
            status, _, body = self.request('GET','/api/entropy-markets')
            self.assertEqual(status,200,body)
            self.assertEqual(json.loads(body)['own_dex'],'io')
            catalog.assert_called_once()
        html = self.request('GET','/')[2].decode()
        self.assertIn('id="entropy-preset"',html)
        self.assertIn('id="entropy-catalog-button"',html)

    def test_http_boundaries_reject_host_origin_csrf_and_large_bodies(self):
        for headers in ({'Host': 'evil.example'}, {'Origin': 'https://evil.example'},
                        {'Host': self.host, 'Origin': 'null'},
                        {'Host': self.host, 'Sec-Fetch-Site': 'cross-site'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request('GET', '/api/session', headers=headers)[0], 403)
        self.assertEqual(self.request('POST', '/api/preview', '{}',
                                     {'Content-Type': 'application/json'})[0], 403)
        token = json.loads(self.request('GET', '/api/session')[2])['csrf_token']
        headers = {'Content-Type': 'application/json', 'Origin': 'http://' + self.host,
                   'X-CSRF-Token': token}
        self.assertEqual(self.request('POST', '/api/preview', 'x' * 16385, headers)[0], 413)
        self.assertEqual(self.request('POST', '/api/preview', '{}',
                                     dict(headers, **{'Transfer-Encoding': 'chunked'}))[0], 400)
        self.assertEqual(self.request('POST', '/api/sign', '{}', headers)[0], 404)
        self.assertEqual(self.request('GET', '/../../config.yaml')[0], 404)
        with self.assertRaisesRegex(ValueError, 'loopback'):
            from funding_collector.web import make_server
            make_server(self.tmp.name, host='0.0.0.0', port=0)

    def post(self, path, value):
        token = json.loads(self.request('GET', '/api/session')[2])['csrf_token']
        return self.request('POST', path, json.dumps(value),
                            {'Content-Type': 'application/json', 'Origin': 'http://' + self.host,
                             'X-CSRF-Token': token})

    def test_preview_then_explicit_paper_and_persistent_exact_retry(self):
        from unittest.mock import patch
        from test_paper import plan
        import time
        p = plan()
        p['deployments'] = {v: {'book_observed_at_unix_s': time.time()} for v in ('core', 'rh')}
        params = dict(market='ETH', quantity='1', direction='core-long', max_notional_usd='200',
                      max_roundtrip_cost_usd='10', core_fee_bps='0', rh_fee_bps='0',
                      max_book_age_seconds='30', max_unhedged_base='0.5',
                      max_hedge_delay_ms='1000', scenario='partial')
        request = {'intent_id': 'ui-1', 'params': params}
        with patch('funding_collector.web.build_preview', return_value=p) as public:
            status, _, body = self.post('/api/preview', request)
            self.assertEqual(status, 200, body)
            draft = json.loads(body)
            self.assertEqual(draft['preview']['mode'], 'PUBLIC_PREVIEW_TRADING_DISABLED')
            self.assertFalse((Path(self.tmp.name) / 'state.json').exists())
            self.assertEqual(self.post('/api/paper', request)[0], 400)
            request.update(preview_id=draft['preview_id'], assume_empty_simulated=True)
            status, _, body = self.post('/api/paper', request)
            self.assertEqual(status, 200, body)
            result = json.loads(body)
            self.assertEqual(result['status'], 'COMPLETED_SYNTHETIC')
            self.assertEqual(result['synthetic_fill_count'], 4)
            self.assertEqual(float(result['residual_base']), 0)
            durable = (Path(self.tmp.name) / 'state.json').read_bytes()
            # A new service instance reads persisted draft + durable ledger.
            from funding_collector.web import App
            self.server.app = App(self.tmp.name)
            self.assertEqual(self.post('/api/paper', request)[0], 200)
            self.assertEqual((Path(self.tmp.name) / 'state.json').read_bytes(), durable)
            public.assert_called_once()
            state = json.loads(self.request('GET', '/api/state')[2])
            self.assertEqual(len(state['intents'][0]['journal']), len(result['journal']))
            request['params'] = dict(params, quantity='2')
            self.assertEqual(self.post('/api/paper', request)[0], 400)

    def test_stale_preview_invalid_parameters_and_public_outage_fail_closed(self):
        from unittest.mock import patch
        from test_paper import plan
        from funding_collector.domain import Reject
        params = dict(market='ETH', quantity='1', direction='core-long', max_notional_usd='200',
                      max_roundtrip_cost_usd='10', core_fee_bps='0', rh_fee_bps='0',
                      max_book_age_seconds='5', max_unhedged_base='0.5',
                      max_hedge_delay_ms='1000', scenario='partial')
        request = {'intent_id': 'stale', 'params': params}
        p = plan()
        p['deployments'] = {v: {'book_observed_at_unix_s': 1} for v in ('core', 'rh')}
        with patch('funding_collector.web.build_preview', return_value=p):
            draft = json.loads(self.post('/api/preview', request)[2])
        request.update(preview_id=draft['preview_id'], assume_empty_simulated=True)
        self.assertIn('expired', json.loads(self.post('/api/paper', request)[2])['error'])
        with patch('funding_collector.web.build_preview', side_effect=Reject('public outage')) as public:
            invalid = dict(params, quantity='NaN')
            self.assertEqual(self.post('/api/preview', {'intent_id': 'bad', 'params': invalid})[0], 400)
            public.assert_not_called()
            self.assertEqual(self.post('/api/preview', {'intent_id': 'outage', 'params': params})[0], 400)
        self.assertFalse((Path(self.tmp.name) / 'state.json').exists())
        self.assertEqual(len(list(Path(self.tmp.name).rglob('error.json'))), 1)

    def test_resume_pending_partial_and_timeout_bounds_are_visible(self):
        from funding_collector.paper import Store, fill
        from test_paper import plan
        with Store(self.tmp.name) as store:
            intent = store.submit('pending', {}, plan(), 'timeout')
            fill(store, intent, 'primary-0', 'core', '0.5')
        status, _, body = self.post('/api/resume', {'intent_id': 'pending'})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result['status'], 'STOPPED_SYNTHETIC')
        self.assertEqual(result['synthetic_fill_count'], 1)
        self.assertEqual(result['pending_unknown_hedge_base'], '0.5')
        self.assertEqual(result['possible_residual_base_bounds'], ['0.0', '0.5'])
        self.assertIn('UNKNOWN', result['stop_reason'])
        self.assertEqual(json.loads(self.post('/api/resume', {'intent_id': 'pending'})[2])['synthetic_fill_count'], 1)

    def test_ui_assets_and_module_serve_entrypoint(self):
        import subprocess
        import sys
        result = subprocess.run([sys.executable, '-m', 'funding_collector', 'serve', '--help'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--port', result.stdout)
        page = self.request('GET', '/')[2].decode()
        for control in ('intent-form', 'strategy_mode', 'primary_venue', 'direction', 'market',
                        'execution', 'quantity', 'primary_fee_bps', 'hedge_fee_bps', 'hedge_venue', 'hedge_market',
                        'preview-button', 'paper-button', 'acknowledge', 'journal'):
            self.assertIn('id="' + control + '"', page)
        self.assertEqual(self.request('GET', '/app.js')[0], 200)
        self.assertEqual(self.request('GET', '/style.css')[0], 200)

    def test_non_ascii_csrf_and_extreme_content_length_fail_closed(self):
        headers = {'Content-Type': 'application/json', 'Origin': 'http://' + self.host,
                   'X-CSRF-Token': '\u00e9' * 32}
        self.assertEqual(self.request('POST', '/api/preview', '{}', headers)[0], 403)
        token = json.loads(self.request('GET', '/api/session')[2])['csrf_token']
        headers.update({'X-CSRF-Token': token, 'Content-Length': '9' * 5000})
        self.assertEqual(self.request('POST', '/api/preview', None, headers)[0], 413)

    def test_health_readiness_session_and_safe_page(self):
        status, headers, body = self.request('GET', '/api/health')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['trading_enabled'], False)
        self.assertTrue(json.loads(self.request('GET', '/api/ready')[2])['ready'])
        status, headers, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn('Content-Security-Policy', headers)
        self.assertIn('СИНТЕТИЧЕСКИЕ', body.decode())
        session = json.loads(self.request('GET', '/api/session')[2])
        self.assertGreaterEqual(len(session['csrf_token']), 32)
        self.assertFalse((Path(self.tmp.name) / 'state.json').exists())


    def test_generic_http_contract_registry_and_safe_controls(self):
        from funding_collector.web import parameters
        from funding_collector.domain import Reject
        params = dict(leg_a_connection='rh', leg_a_instrument='ETH', leg_a_side='sell', leg_a_fee_bps='0',
                      leg_b_connection='core', leg_b_instrument='ETH', leg_b_side='buy', leg_b_fee_bps='0',
                      quantity='1', max_notional_usd='200', max_roundtrip_cost_usd='10',
                      max_book_age_seconds='30', max_unhedged_base='0.5',
                      max_hedge_delay_ms='1000', scenario='partial')
        self.assertEqual(self.request('GET', '/api/connections')[0], 200,
                         'connection registry route missing')
        self.assertEqual(parameters(params), params)
        for bad in (dict(params, leg_a_connection='https://evil.example'),
                    dict(params, leg_b_side='sell'), dict(params, private_key='NO')):
            with self.assertRaises(Reject):
                parameters(bad)
        page = self.request('GET', '/')[2].decode()
        # Generic HTTP v2 remains valid, but is not the default DN v3 form.
        self.assertIn('id="primary_venue"', page)
        self.assertNotIn('id="leg_a_connection"', page)

    def test_dn_ui_contract_full_limit_then_one_market_and_status(self):
        from unittest.mock import patch
        from test_universal import fixture_registry
        from funding_collector.adapters import Registry, Connection, Deployment, Capabilities
        source, registry = fixture_registry(), Registry()
        for v in ('core', 'rh'):
            registry.register(Connection(v, Deployment(v, 'lighter'), 'paper-' + v, 'paper-' + v),
                              source.public('alpha'), Capabilities(public_market_data=True))
        params = dict(strategy_mode='DN_FULL_LIMIT_MARKET_V3', primary_venue='rh', direction='short',
                      market='ETH', quantity='1', execution='post-only', core_fee_bps='0', rh_fee_bps='0',
                      max_notional_usd='200', max_roundtrip_cost_usd='10', max_book_age_seconds='30',
                      scenario='partial')
        request = dict(intent_id='dn-ui', params=params)
        with patch('funding_collector.cli.default_registry', return_value=registry), patch('time.time', return_value=101):
            status, _, body = self.post('/api/preview', request)
            self.assertEqual(status, 200, body)
            draft = json.loads(body)
            p = draft['preview']
            self.assertEqual(p['schema_version'], 3)
            self.assertEqual(p['legs']['leg_a']['connection_id'], 'rh')
            self.assertEqual(p['legs']['leg_a']['side'], 'sell')
            self.assertEqual(p['legs']['leg_a']['order_type'], 'LIMIT')
            self.assertEqual(p['legs']['leg_b']['order_type'], 'MARKET')
            request.update(preview_id=draft['preview_id'], assume_empty_simulated=True)
            status, _, body = self.post('/api/paper', request)
            self.assertEqual(status, 200, body)
        result = json.loads(body)
        self.assertEqual(result['status'], 'COMPLETED_SYNTHETIC')
        fills = [e for e in result['journal'] if e['kind'] == 'fill']
        self.assertEqual([e['venue'] for e in fills], ['leg_a', 'leg_a', 'leg_b'])
        market = [e for e in result['journal'] if e['kind'] == 'market_hedge_intent']
        self.assertEqual(len(market), 1)
        from decimal import Decimal
        self.assertEqual(Decimal(market[0]['quantity_base']), Decimal('1'))
        self.assertEqual(market[0]['side'], 'buy')
        durable = Path(self.tmp.name, 'state.json').read_bytes()
        self.assertEqual(self.post('/api/resume', {'intent_id': 'dn-ui'})[0], 200)
        self.assertEqual(Path(self.tmp.name, 'state.json').read_bytes(), durable)
        status, _, body = self.request('GET', '/api/connector-status')
        self.assertEqual(status, 200)
        self.assertFalse(any(r['live_ready'] for r in json.loads(body)))
        page = self.request('GET', '/')[2].decode()
        self.assertIn('id="connector-status"', page)
        self.assertNotIn('id="stop-button"', page)
        self.assertNotIn('id="max_hedge_delay_ms"', page)


if __name__ == '__main__':
    unittest.main()
