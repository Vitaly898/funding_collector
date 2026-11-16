"""Independent regression review: synthetic fixtures; no network calls."""
import json
from pathlib import Path
import tempfile
import unittest
from decimal import Decimal as D

from test_domain import metadata, book
from test_paper import plan


class Review(unittest.TestCase):
    def test_sigkill_after_durable_fill_recovers_in_new_cli_process(self):
        import select
        import signal
        import subprocess
        import sys
        from funding_collector.paper import Store
        child = '''
import json, signal, sys
from pathlib import Path
from funding_collector.paper import Store, atomic_json, execute
root, target = sys.argv[1:]
with Store(root) as store:
    original_save = store.save
    def checkpoint():
        intent = store.state['intents']['crash']
        fills = [e for e in intent['journal'] if e['kind'] == 'fill']
        if len(fills) == int(target):
            atomic_json(store.root / 'state.json', store.state)
            print('DURABLE', flush=True)
            signal.pause()
        original_save()
    store.save = checkpoint
    execute(store, store.state['intents']['crash'])
'''
        project = Path(__file__).resolve().parents[1]
        from test_universal import universal_plan
        for label, count in ((label, count) for label in ('legacy-v1', 'universal-v2') for count in (1, 2)):
            with self.subTest(schema=label, durable_fill_count=count), tempfile.TemporaryDirectory(
                    dir='/opt/data/cache/scratch', prefix='fc-crash-') as root:
                with Store(root) as store:
                    if label == 'legacy-v1':
                        store.state['version'] = 1
                        store.state['accounts'] = {'core': 'paper-core', 'rh': 'paper-rh'}
                        store.state['inventory'] = {'core': {}, 'rh': {}}
                    store.submit('crash', {}, plan() if label == 'legacy-v1' else universal_plan(), 'partial')
                proc = subprocess.Popen([sys.executable, '-c', child, root, str(count)],
                                        cwd=project, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
                try:
                    assert proc.stdout is not None
                    self.assertTrue(select.select([proc.stdout], [], [], 10)[0], 'checkpoint timeout')
                    self.assertEqual(proc.stdout.readline().strip(), 'DURABLE')
                    proc.kill()  # real OS SIGKILL, not a Python exception
                    proc.communicate(timeout=10)
                    self.assertEqual(proc.returncode, -signal.SIGKILL)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.communicate(timeout=10)
                state_file = Path(root) / 'state.json'
                saved = json.loads(state_file.read_text())['intents']['crash']
                self.assertEqual(sum(e['kind'] == 'fill' for e in saved['journal']), count)
                # Projections intentionally lag the authoritative atomic state.
                exported = json.loads((Path(root) / 'crash/result.json').read_text())
                self.assertEqual(sum(e['kind'] == 'fill' for e in exported['journal']), count - 1)
                cmd = [sys.executable, '-m', 'funding_collector', 'resume',
                       '--store', root, '--intent-id', 'crash']
                resumed = subprocess.run(cmd, cwd=project, capture_output=True, text=True, timeout=10)
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                result = json.loads((Path(root) / 'crash/result.json').read_text())
                fills = [e for e in result['journal'] if e['kind'] == 'fill']
                self.assertEqual([e['event_id'] for e in fills],
                                 ['primary-0', 'hedge-0', 'primary-1', 'hedge-1'])
                self.assertEqual(result['status'], 'COMPLETED_SYNTHETIC')
                self.assertEqual(D(result['residual_base']), 0)
                durable = state_file.read_bytes()
                retry = subprocess.run(cmd, cwd=project, capture_output=True, text=True, timeout=10)
                self.assertEqual(retry.returncode, 0, retry.stderr)
                self.assertEqual(state_file.read_bytes(), durable)
                self.assertEqual(len(list(Path(root).rglob('public/*.json'))), 0)

    def test_nonfinite_http_cache_age_rejected(self):
        from unittest.mock import patch
        from funding_collector.public import PublicClient
        from funding_collector.domain import Reject
        class Response:
            status = 200
            headers = {'Age': 'NaN'}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return b'{"code":200}'
        with tempfile.TemporaryDirectory() as root:
            with patch('urllib.request.OpenerDirector.open', return_value=Response()):
                with self.assertRaisesRegex(Reject, 'cache age'):
                    PublicClient('core', root).get('orderBookDetails')

    def test_http_error_preserves_actual_body_status_headers(self):
        import io
        from urllib.error import HTTPError
        from unittest.mock import patch
        from funding_collector.public import PublicClient
        from funding_collector.domain import Reject
        from email.message import Message
        body = b'{"code":429,"message":"synthetic rate limit"}'
        headers = Message()
        headers['Retry-After'] = '7'
        error = HTTPError('https://api.rh.lighter.xyz/api/v1/orderBookDetails',
                          429, 'rate limit', headers, io.BytesIO(body))
        with tempfile.TemporaryDirectory() as root:
            with patch('urllib.request.OpenerDirector.open', side_effect=error):
                with self.assertRaises(Reject):
                    PublicClient('rh', root).get('orderBookDetails')
            envelope = json.loads(next(Path(root).glob('*.json')).read_text())
            self.assertEqual(envelope['body'], body.decode())
            self.assertEqual(envelope['status'], 429)
            self.assertEqual(envelope['headers']['Retry-After'], '7')
            self.assertIn('error', envelope)

    def test_valid_sell_hedge_not_rejected_by_unconsumed_tail_price(self):
        from funding_collector.domain import Book, Market, preview
        from funding_collector.paper import Store, execute
        markets = {v: Market(metadata(min_quote_amount='60')) for v in ('core', 'rh')}
        rh = Book({'asks': [{'price': '201', 'remaining_base_amount': '10'}],
                   'bids': [{'price': '199', 'remaining_base_amount': '0.5'},
                            {'price': '99', 'remaining_base_amount': '0.5'}]}, 100)
        p = preview(markets, {'core': book(), 'rh': rh}, 'ETH', 'core-long',
                    '1', '300', '200', '0', '0', 101, 5)
        with tempfile.TemporaryDirectory() as root, Store(root) as store:
            intent = store.submit('x', {}, p, 'partial')
            execute(store, intent)
            self.assertEqual([e['venue'] for e in intent['journal'] if e['kind'] == 'fill'],
                             ['core', 'rh', 'core'])
            self.assertEqual(intent['status'], 'STOPPED_SYNTHETIC')
            self.assertEqual(D(intent['residual_base']), D('0.5'))

    def test_hedge_min_notional_uses_consumed_partial_not_whole_leg_limit(self):
        from funding_collector.domain import Book, Market, preview
        from funding_collector.paper import Store, execute
        markets = {v: Market(metadata(min_quote_amount='60')) for v in ('core', 'rh')}
        rh = Book({'asks': [{'price': '101', 'remaining_base_amount': '0.5'},
                            {'price': '201', 'remaining_base_amount': '0.5'}],
                   'bids': [{'price': '99', 'remaining_base_amount': '10'}]}, 100)
        p = preview(markets, {'core': book(), 'rh': rh}, 'ETH', 'rh-long',
                    '1', '300', '100', '0', '0', 101, 5)
        with tempfile.TemporaryDirectory() as root, Store(root) as store:
            intent = store.submit('x', {}, p, 'partial')
            execute(store, intent)
            self.assertEqual(intent['status'], 'STOPPED_SYNTHETIC')
            self.assertEqual([e['venue'] for e in intent['journal'] if e['kind'] == 'fill'], ['core'])
            self.assertEqual(D(intent['residual_base']), D('-0.5'))
