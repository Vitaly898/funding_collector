"""Small stdlib loopback UI. No signing, credentials or account endpoints."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import secrets
import re
import time
import uuid
from types import SimpleNamespace
from typing import cast

from .cli import build_preview, summary, LEG_FIELDS, RISK_FIELDS, DN_FIELDS, DN_PAIR_FIELDS, validate_values
from .lighter import default_registry
from .domain import Reject, number
from .paper import SCENARIOS, Store, atomic_json, execute, serial


LEGACY_FIELDS = ('market', 'quantity', 'direction', 'max_notional_usd', 'max_roundtrip_cost_usd',
                 'core_fee_bps', 'rh_fee_bps', 'max_book_age_seconds', 'max_unhedged_base',
                 'max_hedge_delay_ms', 'scenario')  # HTTP v1 backwards compatibility only
FIELDS = LEG_FIELDS + RISK_FIELDS + ('scenario',)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', value):
        raise Reject('invalid intent id')
    return value


def parameters(value):
    if not isinstance(value, dict) or set(value) not in (set(FIELDS), set(LEGACY_FIELDS), set(DN_FIELDS), set(DN_PAIR_FIELDS)):
        raise Reject('provide exactly the documented intent, fee and risk fields')
    if any(not isinstance(v, str) or not 1 <= len(v) <= 80 for v in value.values()):
        raise Reject('parameters must be bounded decimal/text strings')
    symbols = ('leg_a_instrument', 'leg_b_instrument') if 'leg_a_connection' in value else ('market',)
    if any(not re.fullmatch(r'(?:io:)?[A-Za-z0-9_.-]{1,30}', value[key]) for key in symbols):
        raise Reject('invalid exact instrument symbol')
    if value['scenario'] not in SCENARIOS:
        raise Reject('invalid synthetic scenario')
    validate_values(value)
    return dict(value)



class App:
    def __init__(self, root):
        self.root = Path(root)

    def result(self, intent, store):
        return dict(summary(intent, store.root), journal=intent['journal'],
                    payload=intent['payload'], inventory=store.state['inventory'])

    def state(self):
        with Store(self.root) as store:
            return {'mode': 'SYNTHETIC_ONLY', 'actual_accounts_read': False,
                    'inventory': store.state['inventory'],
                    'intents': [self.result(i, store) for i in store.state['intents'].values()]}

    def action(self, route, data):
        if not isinstance(data, dict):
            raise Reject('JSON object required')
        allowed = {'/api/preview': {'intent_id', 'params'},
                   '/api/paper': {'intent_id', 'params', 'preview_id', 'assume_empty_simulated'},
                   '/api/resume': {'intent_id'}}
        if set(data) != allowed[route]:
            raise Reject('incorrect fields for local action')
        ident = identifier(data['intent_id'])
        with Store(self.root) as store:
            if route == '/api/resume':
                intent = store.state['intents'].get(ident)
                if not intent:
                    raise Reject('unknown intent')
                return self.result(execute(store, intent), store)
            payload = parameters(data['params'])
            payload['assume_empty_simulated'] = True
            old = store.existing(ident, payload)
            if route == '/api/preview':
                if old:
                    raise Reject('intent already submitted; restore it from journal, do not preview again')
                artifacts = store.root / ident / 'attempts' / uuid.uuid4().hex
                artifacts.mkdir(parents=True, exist_ok=False)
                try:
                    p = build_preview(SimpleNamespace(**payload), artifacts)
                except (Reject, OSError, ValueError, KeyError, TypeError) as error:
                    atomic_json(artifacts / 'error.json', {'status': 'REJECTED', 'error': str(error)})
                    raise
                draft = {'intent_id': ident, 'params': data['params'], 'preview_id': uuid.uuid4().hex,
                         'preview': serial(p)}
                atomic_json(store.root / ident / 'ui-draft.json', draft)
                return draft
            if data['assume_empty_simulated'] is not True:
                raise Reject('explicit EMPTY LOCAL SIMULATION acknowledgement required')
            path = store.root / ident / 'ui-draft.json'
            if not path.is_file():
                raise Reject('public preview required before paper')
            draft = json.loads(path.read_text())
            if draft['params'] != data['params'] or draft['preview_id'] != data['preview_id']:
                raise Reject('preview changed or parameters do not match; preview again')
            if old:
                return self.result(execute(store, old), store)
            p = draft['preview']
            for venue in p['legs']:
                age = time.time() - p['deployments'][venue]['book_observed_at_unix_s']
                if not 0 <= age <= float(payload['max_book_age_seconds']):
                    raise Reject('preview receipt/cache age expired; fetch a new public preview')
            intent = store.submit(ident, payload, p, payload['scenario'])
            return self.result(execute(store, intent), store)



class LocalServer(HTTPServer):
    store: Path
    csrf: str
    app: App
    live_root: Path | None

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address


class Handler(BaseHTTPRequestHandler):
    server_version = 'FundingCollectorLocal/1'

    @property
    def local(self):
        return cast(LocalServer, self.server)

    def log_message(self, format, *args):
        pass  # never log request bodies, arbitrary headers or query strings

    def send(self, status, value, content_type='application/json; charset=utf-8'):
        data = value.encode('utf-8') if isinstance(value, str) else json.dumps(value, default=str).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        self.wfile.write(data)

    def allowed(self, mutation=False):
        hosts = self.headers.get_all('Host', [])
        expected = '127.0.0.1:' + str(self.local.server_port)
        if hosts != [expected]:
            return False
        origins = self.headers.get_all('Origin', [])
        if origins and origins != ['http://' + expected]:
            return False
        if self.headers.get('Sec-Fetch-Site') not in (None, 'same-origin', 'none'):
            return False
        if mutation:
            tokens = self.headers.get_all('X-CSRF-Token', [])
            return (origins == ['http://' + expected] and len(tokens) == 1
                    and secrets.compare_digest(tokens[0].encode('utf-8'), self.local.csrf.encode('ascii')))
        return True

    def do_POST(self):
        self.close_connection = True
        if not self.allowed(mutation=True):
            return self.send(403, {'error': 'Host/Origin/CSRF refused; reload UI on 127.0.0.1'})
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            return self.send(400, {'error': 'one Content-Length required; transfer encoding forbidden'})
        if len(lengths[0]) > 8:
            return self.send(413, {'error': 'Content-Length exceeds body limit'})
        length = int(lengths[0])
        if length > 16384:
            return self.send(413, {'error': 'body exceeds 16384 bytes'})
        if self.headers.get('Content-Type') != 'application/json':
            return self.send(415, {'error': 'application/json required'})
        if self.path not in ('/api/preview', '/api/paper', '/api/resume'):
            return self.send(404, {'error': 'unknown local route'})
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise Reject('truncated body')
            data = json.loads(raw)
            result = self.local.app.action(self.path, data)
            return self.send(200, result)
        except (Reject, OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
            return self.send(400, {'status': 'REJECTED', 'error': str(error), 'trading_enabled': False})

    def do_GET(self):
        if not self.allowed():
            return self.send(403, {'error': 'Host/Origin refused; open literal 127.0.0.1 URL'})
        if self.path == '/api/health':
            return self.send(200, {'ok': True, 'trading_enabled': False})
        if self.path == '/api/ready':
            try:
                with Store(self.local.store):
                    pass
                return self.send(200, {'ready': True, 'public_connectivity_checked': False})
            except (Reject, OSError, ValueError):
                return self.send(503, {'ready': False})
        if self.path == '/api/state':
            try:
                return self.send(200, self.local.app.state())
            except (Reject, OSError, ValueError, KeyError, TypeError) as error:
                return self.send(503, {'error': str(error), 'trading_enabled': False})
        if self.path == '/api/live-worker-status':
            if self.local.live_root is None:
                return self.send(200, {'projection': 'NOT_CONFIGURED', 'trading_enabled': False})
            try:
                from .supervised import status
                return self.send(200, status(self.local.live_root))
            except Exception:
                return self.send(503, {'projection': 'UNAVAILABLE', 'trading_enabled': False})
        if self.path == '/api/live-foundation-status':
            from .live_status import foundation_status
            return self.send(200, foundation_status())
        if self.path == '/api/connector-status':
            from .original_dn import connector_status
            return self.send(200, connector_status())
        if self.path == '/api/connections':
            return self.send(200, default_registry(self.local.store / 'public').describe())
        if self.path == '/api/hl-connection-status':
            from .hyperliquid import connection_status
            return self.send(200, connection_status())
        if self.path == '/api/hyperliquid-markets':
            try:
                adapter = default_registry(self.local.store / 'public').public('hyperliquid')
                return self.send(200, adapter.catalog())
            except (Reject, OSError, ValueError, KeyError, TypeError) as error:
                return self.send(503, {'error': str(error), 'trading_enabled': False})
        if self.path == '/api/entropy-markets':
            try:
                adapter = default_registry(self.local.store / 'public').public('entropy')
                return self.send(200, adapter.catalog())
            except (Reject, OSError, ValueError, KeyError, TypeError) as error:
                return self.send(503, {'error': str(error), 'trading_enabled': False})
        if self.path == '/api/session':
            return self.send(200, {'csrf_token': self.local.csrf})
        assets = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'),
                  '/style.css': ('style.css', 'text/css')}
        if self.path in assets:
            name, mime = assets[self.path]
            return self.send(200, (Path(__file__).parent / 'ui' / name).read_text(encoding='utf-8'), mime + '; charset=utf-8')
        return self.send(404, {'error': 'unknown local route'})


def make_server(store, host='127.0.0.1', port=8765, *, live_root=None):
    if host != '127.0.0.1':
        raise Reject('UI bind must be literal IPv4 loopback 127.0.0.1')
    server = LocalServer((host, port), Handler)
    server.store = Path(store)
    server.app = App(store)
    server.live_root = Path(live_root) if live_root is not None else None
    server.csrf = secrets.token_urlsafe(32)
    return server


def serve(store, host='127.0.0.1', port=8765, *, live_root=None):
    if not 0 <= port <= 65535:
        raise Reject('port outside 0..65535')
    with make_server(store, host, port, live_root=live_root) as server:
        print(f'Локальный UI: http://127.0.0.1:{server.server_port}/ · PUBLIC + SYNTHETIC ONLY; trading disabled', flush=True)
        print(f'Paper ledger: {Path(store).resolve()}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print('UI остановлен; ledger сохранён. Автоматическое flatten отсутствует.', flush=True)
    return 0
