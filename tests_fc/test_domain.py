"""Synthetic fixtures only; no network or original DN imports."""
import importlib.util
import unittest
from decimal import Decimal as D


class Bootstrap(unittest.TestCase):
    def test_isolated_package_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('funding_collector'))


class ExecutableDepth(unittest.TestCase):
    def test_walk_sorts_remaining_orders_and_checks_age_depth(self):
        from funding_collector.domain import Book, Reject
        b = Book({'asks': [{'price': '102', 'remaining_base_amount': '2'},
                           {'price': '100', 'remaining_base_amount': '1'}],
                  'bids': [{'price': '99', 'remaining_base_amount': '3'}]}, 100)
        self.assertEqual(b.walk('buy', D('2'), 101, 5)['vwap'], D('101'))
        self.assertEqual(b.walk('sell', D('2'), 101, 5)['vwap'], D('99'))
        with self.assertRaisesRegex(Reject, 'depth'):
            b.walk('buy', D('4'), 101, 5)
        with self.assertRaisesRegex(Reject, 'stale'):
            b.walk('buy', D('1'), 106, 5)
        with self.assertRaises(Reject):
            b.walk('buy', D('NaN'), 101, 5)


def metadata(symbol='ETH', **overrides):
    data = dict(symbol=symbol, market_id=0, market_type='perp', status='active',
                min_base_amount='0.01', min_quote_amount='1', multiplier='1',
                supported_size_decimals=2, supported_price_decimals=2)
    return dict(data, **overrides)


def book(at=100):
    from funding_collector.domain import Book
    return Book({'asks': [{'price': '101', 'remaining_base_amount': '10'}],
                 'bids': [{'price': '99', 'remaining_base_amount': '10'}]}, at)


class Preview(unittest.TestCase):
    def test_limit_price_notional_cannot_exceed_risk_cap(self):
        from funding_collector.domain import Book, Market, preview, Reject
        markets = {v: Market(metadata()) for v in ('core', 'rh')}
        shallow = Book({'asks': [{'price': '100', 'remaining_base_amount': '0.99'},
                                 {'price': '200', 'remaining_base_amount': '1'}],
                        'bids': [{'price': '99', 'remaining_base_amount': '10'}]}, 100)
        with self.assertRaisesRegex(Reject, 'notional'):
            preview(markets, {'core': shallow, 'rh': book()}, 'ETH', 'core-long',
                    '1', '150', '100', '0', '0', 101, 5)

    def test_explicit_constraints_costs_direction_and_units(self):
        from funding_collector.domain import Market, preview, Reject
        markets = {v: Market(metadata()) for v in ('core', 'rh')}
        books = {v: book() for v in markets}
        result = preview(markets, books, 'ETH', 'core-long', '1', '200', '10', '5', '2', 101, 5)
        self.assertEqual(result['legs']['core']['side'], 'buy')
        self.assertEqual(result['legs']['rh']['side'], 'sell')
        self.assertEqual(result['entry_fee_usd'], D('0.0703'))
        self.assertEqual(result['roundtrip_cost_usd'], D('4.14'))
        for qty, cap, cost in [('0.001', '200', '10'), ('0.015', '200', '10'),
                               ('3', '200', '10'), ('1', '200', '1')]:
            with self.subTest(qty=qty, cap=cap, cost=cost):
                with self.assertRaises(Reject):
                    preview(markets, books, 'ETH', 'core-long', qty, cap, cost, '5', '2', 101, 5)
        with self.assertRaisesRegex(Reject, 'min notional'):
            Market(metadata(min_quote_amount='200')).validate(D('1'), D('101'))
        with self.assertRaisesRegex(Reject, 'price precision'):
            markets['core'].validate(D('1'), D('101.001'))
        with self.assertRaises(Reject):
            Market(metadata(multiplier='1000'))


if __name__ == '__main__':
    unittest.main()
