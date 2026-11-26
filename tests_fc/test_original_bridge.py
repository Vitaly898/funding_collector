"""Offline original DN reuse contracts; never initialize/authenticate SDKs."""
import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from funding_collector.domain import Reject


class OriginalBridge(unittest.TestCase):
    def test_lazy_factory_reuses_each_original_class_without_initialization(self):
        from funding_collector.original_dn import load_original, ORIGINALS
        for venue, (module, name, _) in ORIGINALS.items():
            with self.subTest(venue=venue):
                fake = types.ModuleType(module)
                constructor = unittest.mock.Mock()
                setattr(fake, name, constructor)
                with patch.dict(sys.modules, {module: fake}):
                    bridge = load_original(venue, instrument_map={'ETH-PERP': 'ETH'})
                constructor.assert_called_once_with(instrument_map={'ETH-PERP': 'ETH'})
                self.assertIs(bridge.adapter, constructor.return_value)
                bridge.adapter.initialize.assert_not_called()

    def test_gated_delegation_and_no_market_or_ack_as_fill(self):
        from funding_collector.original_dn import OriginalDNBridge
        adapter = types.SimpleNamespace(place_limit_order=AsyncMock(return_value='ack'),
                                        get_position=AsyncMock(return_value='position'),
                                        get_open_orders=AsyncMock(return_value=['working']))
        bridge = OriginalDNBridge('nado', adapter)
        with self.assertRaisesRegex(Reject, 'authority'):
            asyncio.run(bridge.limit('ETH-PERP', 'buy', '100', '0.02', external_id='x'))
        adapter.place_limit_order.assert_not_called()
        auth = unittest.mock.Mock()
        self.assertEqual(asyncio.run(bridge.limit('ETH-PERP', 'sell', '100', '0.02',
                         external_id='x', authority=auth)), 'ack')
        from controller.models import Side
        adapter.place_limit_order.assert_awaited_once_with('ETH-PERP', Side.SELL, 100.0, 0.02,
                                       post_only=True, reduce_only=False, external_id='x')
        auth.authorize.assert_called_once_with('nado', 'limit', 'ETH-PERP', '0.02')
        with self.assertRaisesRegex(Reject, 'MARKET'):
            asyncio.run(bridge.market('ETH-PERP', 'buy', '0.02', authority=auth))
        with self.assertRaisesRegex(Reject, 'confirmed fills'):
            bridge.confirmed_fills('x')
        self.assertEqual(asyncio.run(bridge.reconcile('ETH-PERP', authority=auth)),
                         {'position': 'position', 'working_orders': ['working'],
                          'fills_reconciled': False})

    def test_original_variational_payload_with_fake_client_no_auth(self):
        from funding_collector.original_dn import load_original
        # Stub auth dependencies only; execute the actual original adapter method.
        deps = {}
        for module, attr in [('dotenv', 'load_dotenv'), ('eth_account', 'Account'),
                             ('eth_account.messages', 'encode_defunct')]:
            deps[module] = types.ModuleType(module)
            setattr(deps[module], attr, unittest.mock.Mock(side_effect=AssertionError('no auth')))
        with patch.dict(sys.modules, deps):
            bridge = load_original('variational', instrument_map={'ETH-PERP': 'ETH'})
        client = types.SimpleNamespace(create_limit_order=AsyncMock(return_value={'rfq_id': 'fixture'}))
        bridge.adapter._client = client
        bridge.adapter._min_qty_cache['ETH'] = 0.001
        result = asyncio.run(bridge.limit('ETH-PERP', 'buy', '100', '0.02',
                            external_id='dn-1', authority=unittest.mock.Mock()))
        self.assertTrue(result.success)
        payload = client.create_limit_order.call_args.args[0]
        self.assertEqual(payload['order_type'], 'limit')
        self.assertEqual(payload['qty'], '0.02')
        self.assertEqual(payload['instrument']['underlying'], 'ETH')
        self.assertEqual(payload['client_order_id'], 'dn-1')
        self.assertFalse(payload['is_reduce_only'])
        self.assertFalse(payload['is_auto_resize'])
        # An accepted original response remains just an ack, never a confirmed fill.
        with self.assertRaisesRegex(Reject, 'confirmed fills'):
            bridge.confirmed_fills(result.id)

    def test_original_extended_with_fake_sdk_preserves_order_flags(self):
        from decimal import Decimal
        from controller.models import Side
        from funding_collector.original_dn import load_original
        deps = {}
        for module, attr, value in [('bot.config', 'ExtendedBotConfig', object),
                                  ('bot.trading_bot', 'ExtendedTradingBot', object),
                                  ('x10.perpetual.orders', 'OrderSide', Side),
                                  ('x10.perpetual.positions', 'PositionSide', types.SimpleNamespace(LONG='long'))]:
            deps[module] = types.ModuleType(module)
            setattr(deps[module], attr, value)
        with patch.dict(sys.modules, deps):
            bridge = load_original('extended', instrument_map={'ETH-PERP': 'ETH-USD'})
        tc = types.SimpleNamespace(round_price=lambda n:n, round_order_size=lambda n:n)
        orders = types.SimpleNamespace(place_order=AsyncMock(return_value=types.SimpleNamespace(data=types.SimpleNamespace(id=42))))
        bridge.adapter._bot = types.SimpleNamespace(markets=types.SimpleNamespace(
            find_market=AsyncMock(return_value=types.SimpleNamespace(trading_config=tc))), orders=orders)
        bridge.adapter.get_open_orders = AsyncMock(return_value=[types.SimpleNamespace(id='ext:42')])
        result = asyncio.run(bridge.limit('ETH-PERP', 'sell', '100', '0.02', external_id='dn-ext',
                                         authority=unittest.mock.Mock()))
        self.assertEqual(result.id, 'ext:42')
        self.assertTrue(result.success)
        orders.place_order.assert_awaited_once_with(market_name='ETH-USD', amount=Decimal('0.02'),
            price=Decimal('100.0'), side=Side.SELL, post_only=True, reduce_only=False, external_id='dn-ext')
        bridge.adapter.get_open_orders.assert_awaited_once_with('ETH-PERP')

    def test_original_nado_with_fake_sdk_preserves_x18_and_post_only(self):
        from funding_collector.original_dn import load_original
        math_module = types.ModuleType('nado_protocol.utils.math')
        math_module.round_x18 = lambda n, step: n - n % step
        bridge = load_original('nado', instrument_map={'ETH-PERP': 2})
        client = types.SimpleNamespace(get_book_info=unittest.mock.Mock(return_value=types.SimpleNamespace(
            price_increment_x18=10**16, size_increment=10**15)),
            place_post_only_order=unittest.mock.Mock(return_value=('fixture', None)))
        bridge.adapter._client = client
        with patch.dict(sys.modules, {'nado_protocol.utils.math': math_module}):
            result = asyncio.run(bridge.limit('ETH-PERP', 'sell', '100', '0.02',
                        external_id='dn-nado', authority=unittest.mock.Mock()))
        self.assertTrue(result.success)
        self.assertEqual(result.id, 'nado:fixture')
        client.place_post_only_order.assert_called_once_with(2, 100 * 10**18, -2 * 10**16, False)
        # Original Nado post-only ignores external_id; no exactly-once promise.

    def test_lighter_core_rh_registry_uses_single_shared_implementation(self):
        from funding_collector.lighter import LighterCoreRH, default_registry
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            registry = default_registry(root)
            for venue in ('core', 'rh'):
                self.assertIsInstance(registry.public(venue), LighterCoreRH)
                self.assertFalse(registry.capabilities(venue).execution)

    def test_readiness_is_honest_and_does_not_import_or_initialize_originals(self):
        from funding_collector.original_dn import connector_status
        with patch('importlib.import_module', side_effect=AssertionError('no imports')):
            rows = connector_status()
        self.assertEqual({r['id'] for r in rows}, {'extended', 'nado', 'variational', 'core', 'rh', 'aster'})
        for row in rows:
            self.assertFalse(row['live_ready'])
            self.assertFalse(row['account_checked'])
            self.assertTrue(row['blockers'])
        self.assertEqual({r['id'] for r in rows if r['paper_selectable']}, {'core', 'rh', 'extended', 'aster'})


if __name__ == '__main__':
    unittest.main()
