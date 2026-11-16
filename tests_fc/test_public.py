"""Transport contract tests use explicitly synthetic HTTP responses."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class PublicTransport(unittest.TestCase):
    def test_deployments_and_trading_are_fail_closed(self):
        from funding_collector.public import PublicClient, DEPLOYMENTS
        from funding_collector.domain import Reject
        self.assertEqual(DEPLOYMENTS['core'].chain_id, 304)
        self.assertEqual(DEPLOYMENTS['rh'].chain_id, 466324)
        with tempfile.TemporaryDirectory() as root:
            client = PublicClient('rh', Path(root))
            with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden')):
                for action in ('create', 'cancel', 'withdraw', 'sign', 'POST', 'account', 'sendTx'):
                    with self.subTest(action=action):
                        with self.assertRaises(Reject):
                            client.request(action)
                with self.assertRaises(Reject):
                    client.get('orderBookOrders', market_id=-1, limit=100)
                with self.assertRaises(Reject):
                    client.get('orderBookDetails', account_index=1)
                with self.assertRaises(Reject):
                    client.create_order()
                with self.assertRaises(Reject):
                    client.cancel_order()
                with self.assertRaises(Reject):
                    client.withdraw()
                with self.assertRaises(Reject):
                    client.sign()

    def test_metadata_resolution_is_active_exact_perp_not_spot(self):
        from funding_collector.public import resolve_market
        from funding_collector.domain import Reject
        from test_domain import metadata
        raw = {'code': 200, 'order_book_details': [metadata('ETH2'), metadata('ETH')],
               'spot_order_book_details': [metadata('ETH', market_id=2048)]}
        self.assertEqual(resolve_market(raw, 'ETH').market_id, 0)
        with self.assertRaises(Reject):
            resolve_market(raw, 'BTC')
        raw['order_book_details'].append(metadata('ETH'))
        with self.assertRaises(Reject):
            resolve_market(raw, 'ETH')

    def test_real_transport_preserves_raw_response_and_timestamp(self):
        from funding_collector.public import PublicClient
        class Response:
            status = 200
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return b'{"code":200,"asks":[],"bids":[]}'
        with tempfile.TemporaryDirectory() as root:
            with patch('urllib.request.OpenerDirector.open', return_value=Response()) as opened:
                raw, observed = PublicClient('core', Path(root)).get('orderBookOrders', market_id=0, limit=100)
            request = opened.call_args.args[0]
            self.assertEqual(request.get_method(), 'GET')
            self.assertIn('mainnet.zklighter.elliot.ai/api/v1/orderBookOrders?', request.full_url)
            self.assertEqual(raw['code'], 200)
            envelope = json.loads(next(Path(root).glob('*.json')).read_text())
            self.assertIn('received_at_unix_s', envelope)
            self.assertLessEqual(observed, envelope['received_at_unix_s'])
            self.assertEqual(json.loads(envelope['body']), raw)

    def test_cache_age_uses_seconds_and_rejects_cached_books(self):
        from funding_collector.public import PublicClient
        from funding_collector.domain import Book, Reject
        class Response:
            status = 200
            headers = {'Date': 'Thu, 01 Jan 1970 00:01:40 GMT', 'Age': '10'}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit):
                return b'{"code":200,"asks":[{"price":"101","remaining_base_amount":"1"}],"bids":[{"price":"99","remaining_base_amount":"1"}]}'
        with tempfile.TemporaryDirectory() as root:
            with patch('urllib.request.OpenerDirector.open', return_value=Response()), patch('time.time', return_value=102):
                raw, observed = PublicClient('core', Path(root)).get('orderBookOrders', market_id=0, limit=100)
            self.assertEqual(observed, 92)  # Age=10 seconds, not milliseconds or event time.
            with self.assertRaisesRegex(Reject, 'stale'):
                Book(raw, observed).walk('buy', '1', 102, 5)

    def test_outage_is_recorded_without_fabricating_data(self):
        from funding_collector.public import PublicClient
        from funding_collector.domain import Reject
        with tempfile.TemporaryDirectory() as root:
            with patch('urllib.request.OpenerDirector.open', side_effect=OSError('synthetic outage')):
                with self.assertRaisesRegex(Reject, 'public GET failed'):
                    PublicClient('rh', Path(root)).get('orderBookDetails')
            envelope = json.loads(next(Path(root).glob('*.json')).read_text())
            self.assertIsNone(envelope['body'])
            self.assertIn('error', envelope)
