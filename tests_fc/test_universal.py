"""Universal contracts. The third adapter below is OBVIOUSLY SYNTHETIC, TESTS ONLY."""
import importlib.util
import tempfile
import unittest
from decimal import Decimal as D

from test_domain import book


class UniversalContract(unittest.TestCase):
    def test_explicit_units_and_failclosed_capabilities(self):
        self.assertIsNotNone(importlib.util.find_spec('funding_collector.adapters'),
                             'universal adapter contract is missing')
        from funding_collector.adapters import Capabilities, Connection, Deployment, Registry
        from funding_collector.domain import Instrument, Reject
        caps = Capabilities(public_market_data=True)
        self.assertFalse(caps.execution)
        with self.assertRaisesRegex(Reject, 'capability'):
            caps.require('ioc')
        with self.assertRaises(Reject):
            caps.require('made_up')
        with self.assertRaises(Reject):
            Instrument('ETH', 'x', 'ETH', 'USD', 'inverse', 'base', '1', True,
                       '0.01', '0.01', '0.01', '1')
        with self.assertRaises(Reject):
            Instrument('ETH', 'x', 'ETH', 'USD', 'linear_perpetual', 'contracts', '1000', True,
                       '0.01', '0.01', '0.01', '1')
        with self.assertRaises(Reject):
            Instrument('ETH', 'x', 'ETH', 'USD', 'linear_perpetual', 'base', '1', False,
                       '0.01', '0.01', '0.01', '1')
        deployment = Deployment('fixture-net', 'SYNTHETIC_TEST_ONLY')
        conn = Connection('fixture-account', deployment, 'fixture-owner', 'paper-fixture')
        registry = Registry()
        registry.register(conn, object(), caps)
        self.assertEqual(registry.connection('fixture-account'), conn)
        with self.assertRaisesRegex(Reject, 'duplicate'):
            registry.register(conn, object(), caps)
        with self.assertRaisesRegex(Reject, 'unknown connection'):
            registry.connection('https://evil.example')


class SyntheticThirdAdapter:
    """NOT an exchange integration. No network. Lives exclusively in tests."""
    def snapshot(self, symbol):
        from funding_collector.domain import Instrument
        return (Instrument(symbol, 'fixture-instrument-7', 'ETH', 'USD',
                           'linear_perpetual', 'base', '1', True,
                           '0.01', '0.01', '0.01', '1'), book(),
                {'synthetic_test_only': True, 'book_observed_at_unix_s': 100})


def fixture_registry():
    from funding_collector.adapters import Capabilities, Connection, Deployment, Registry
    r = Registry()
    # Three distinctly identified venues/deployments/accounts; no engine edits.
    for name in ('alpha', 'beta', 'third-SYNTHETIC-ONLY'):
        r.register(Connection(name, Deployment(name + '-net', 'TEST_' + name),
                              name + '-owner', 'paper-' + name),
                   SyntheticThirdAdapter(), Capabilities(public_market_data=True))
    return r


def universal_plan(a='third-SYNTHETIC-ONLY', b='alpha', side_a='sell', side_b='buy'):
    from funding_collector.domain import HedgeIntent, Leg, preview_intent
    return preview_intent(HedgeIntent(Leg(a, 'ETH-test', side_a, '5'),
                                     Leg(b, 'ETH-other', side_b, '2'), '1'),
                          fixture_registry(), '200', '10', 101, 5)


class UniversalExecution(unittest.TestCase):
    def test_third_adapter_two_explicit_legs_without_engine_changes(self):
        import funding_collector.domain as domain
        self.assertTrue(hasattr(domain, 'HedgeIntent'), 'explicit two-leg intent missing')
        from funding_collector.paper import Store, execute
        from pathlib import Path
        p = universal_plan()
        self.assertEqual(p['roles'], {'primary': 'leg_a', 'hedge': 'leg_b'})
        self.assertEqual(p['legs']['leg_a']['connection_id'], 'third-SYNTHETIC-ONLY')
        self.assertEqual(p['legs']['leg_b']['side'], 'buy')
        self.assertEqual(p['entry_fee_usd'], D('0.0697'))
        with tempfile.TemporaryDirectory() as root:
            with Store(root) as store:
                result = execute(store, store.submit('universal', {'explicit': True}, p, 'partial'))
                self.assertEqual(result['status'], 'COMPLETED_SYNTHETIC')
                fills = [e for e in result['journal'] if e['kind'] == 'fill']
                self.assertEqual([e['venue'] for e in fills], ['leg_a', 'leg_b'] * 2)
                self.assertEqual([e['connection_id'] for e in fills],
                                 ['third-SYNTHETIC-ONLY', 'alpha'] * 2)
                self.assertEqual(D(result['residual_base']), 0)
                self.assertEqual(D(store.state['inventory']['paper-third-SYNTHETIC-ONLY']['ETH']), -1)
            state = Path(root, 'state.json').read_bytes()
            with Store(root) as store:
                execute(store, store.submit('universal', {'explicit': True}, p, 'partial'))
            self.assertEqual(Path(root, 'state.json').read_bytes(), state)


class UniversalInterfaces(unittest.TestCase):
    def test_generic_cli_uses_selected_connections_and_exact_retry_offline(self):
        from funding_collector.cli import parser, main
        import contextlib
        import io
        import json
        from pathlib import Path
        from unittest.mock import patch
        self.assertIn('--leg-a-connection', parser().format_help() +
                      parser()._subparsers._group_actions[0].choices['paper'].format_help())
        with tempfile.TemporaryDirectory() as root:
            args = ['paper', '--store', root, '--intent-id', 'generic-cli',
                    '--leg-a-connection', 'rh', '--leg-a-instrument', 'ETH', '--leg-a-side', 'sell',
                    '--leg-b-connection', 'core', '--leg-b-instrument', 'ETH', '--leg-b-side', 'buy',
                    '--leg-a-fee-bps', '5', '--leg-b-fee-bps', '2', '--quantity', '1',
                    '--max-notional-usd', '200', '--max-roundtrip-cost-usd', '10',
                    '--max-unhedged-base', '0.5', '--max-hedge-delay-ms', '1000',
                    '--assume-empty-simulated']
            with patch('funding_collector.cli.build_preview', return_value=universal_plan()):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(main(args), 0)
                self.assertEqual(json.loads(out.getvalue())['status'], 'COMPLETED_SYNTHETIC')
            durable = Path(root, 'state.json').read_bytes()
            with patch('funding_collector.cli.build_preview', side_effect=AssertionError('network')):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(args), 0)
            self.assertEqual(Path(root, 'state.json').read_bytes(), durable)

    def test_v1_read_retry_and_atomic_explicit_migration_preserve_history(self):
        from funding_collector.paper import Store, execute, atomic_json
        from test_paper import plan
        from pathlib import Path
        import json
        with tempfile.TemporaryDirectory() as root:
            with Store(root) as store:
                result = execute(store, store.submit('old', {'quantity': '1'}, plan(), 'partial'))
                original = json.loads(json.dumps(store.state))
            # Golden v1 semantics: fixed inventory/account names; no generic roles.
            original['version'] = 1
            original['accounts'] = {'core': 'paper-core', 'rh': 'paper-rh'}
            for event in original['intents']['old']['journal']:
                event.pop('connection_id', None)
            atomic_json(Path(root, 'state.json'), original)
            before = Path(root, 'state.json').read_bytes()
            with Store(root) as store:
                self.assertTrue(hasattr(store, 'migrate'), 'explicit atomic migration missing')
                execute(store, store.existing('old', {'quantity': '1'}))
            self.assertEqual(Path(root, 'state.json').read_bytes(), before)
            with Store(root) as store:
                store.migrate()
            after = json.loads(Path(root, 'state.json').read_text())
            self.assertEqual(after['version'], 2)
            for key in ('accounts', 'inventory', 'intents'):
                self.assertEqual(after[key], original[key])
            migrated = Path(root, 'state.json').read_bytes()
            with Store(root) as store:
                store.migrate()
                execute(store, store.existing('old', {'quantity': '1'}))
            self.assertEqual(Path(root, 'state.json').read_bytes(), migrated)


class UniversalSafety(unittest.TestCase):
    def test_snapshot_clock_is_checked_after_adapter_read_not_before(self):
        from funding_collector.cli import build_preview
        from types import SimpleNamespace
        from pathlib import Path
        from unittest.mock import patch
        from funding_collector.domain import Reject
        args = SimpleNamespace(leg_a_connection='third-SYNTHETIC-ONLY', leg_a_instrument='ETH',
                               leg_a_side='buy', leg_a_fee_bps='0', leg_b_connection='alpha',
                               leg_b_instrument='ETH', leg_b_side='sell', leg_b_fee_bps='0',
                               quantity='1', max_notional_usd='200', max_roundtrip_cost_usd='10',
                               max_book_age_seconds='5', max_unhedged_base='0.5', max_hedge_delay_ms='1000')
        registry = fixture_registry()
        # clock 99 is before the fixture's received-at=100, then advances to 101.
        with tempfile.TemporaryDirectory() as root, \
                patch('funding_collector.cli.default_registry', return_value=registry), \
                patch('time.time', side_effect=[99, 101, 101, 101, 101]):
            try:
                result = build_preview(args, Path(root))
                error = None
            except Reject as exc:
                error = str(exc)
            self.assertIsNone(error, 'preview clock was frozen before network reads: ' + str(error))

    def test_partial_chunks_respect_both_nondividing_base_increments(self):
        from funding_collector.domain import HedgeIntent, Leg, preview_intent
        from funding_collector.paper import Store, execute
        registry = fixture_registry()
        for name, step in (('alpha', D('0.02')), ('beta', D('0.03'))):
            adapter = registry.public(name)
            original = adapter.snapshot
            def with_step(symbol, original=original, step=step):
                market, book, provenance = original(symbol)
                market.size_step = step
                return market, book, provenance
            adapter.snapshot = with_step
        p = preview_intent(HedgeIntent(Leg('alpha', 'ETH', 'buy', '0'),
                                      Leg('beta', 'ETH', 'sell', '0'), '0.18'), registry,
                           '200', '10', 101, 5)
        with tempfile.TemporaryDirectory() as root, Store(root) as store:
            result = execute(store, store.submit('increments', {}, p, 'partial'))
            self.assertEqual(result['status'], 'COMPLETED_SYNTHETIC')
            fills = [e for e in result['journal'] if e['kind'] == 'fill']
            self.assertEqual([D(e['quantity']) for e in fills], [D('0.06'), D('0.06'), D('0.12'), D('0.12')])

    def test_optional_components_remain_separate_and_default_capabilities_fail_closed(self):
        from funding_collector.adapters import Capabilities, Connection, Deployment, Registry
        from funding_collector.domain import Reject
        registry = Registry()
        self.assertTrue(hasattr(registry, 'account_reader'), 'optional account contract inaccessible')
        reader, executor = object(), object()  # synthetic contract handles, never called
        registry.register(Connection('optional', Deployment('fixture', 'TEST'), 'owner', 'paper-owner'),
                          SyntheticThirdAdapter(), Capabilities(public_market_data=True, account_read=True,
                                                               execution=True, ioc=True),
                          account_reader=reader, execution=executor)
        self.assertIs(registry.account_reader('optional'), reader)
        self.assertIs(registry.execution('optional'), executor)
        for method in ('account_reader', 'execution'):
            with self.assertRaisesRegex(Reject, 'capability'):
                getattr(fixture_registry(), method)('alpha')

    def test_inventory_scope_cannot_be_rebound_to_another_deployment(self):
        from funding_collector.paper import Store
        from funding_collector.domain import Reject
        p = universal_plan()
        with tempfile.TemporaryDirectory() as root:
            with Store(root) as store:
                # A persisted, still-empty inventory namespace is nevertheless bound.
                store.state['accounts']['paper-alpha'] = {'connection_id': 'alpha',
                    'venue': 'TEST_alpha', 'deployment_id': 'wrong-network', 'account_identity': 'alpha-owner'}
                store.state['inventory']['paper-alpha'] = {}
                with self.assertRaisesRegex(Reject, 'identity conflict'):
                    store.submit('rebound', {}, p, 'partial')
                self.assertNotIn('paper-third-SYNTHETIC-ONLY', store.state['accounts'],
                                 'failed submission mutated unrelated account identity')

    def test_capabilities_cannot_claim_private_execution_without_implementation(self):
        from funding_collector.adapters import Capabilities, Connection, Deployment, Registry
        from funding_collector.domain import Reject
        with self.assertRaisesRegex(Reject, 'capability'):
            Registry().register(Connection('x', Deployment('net', 'venue'), 'account', 'paper-x'),
                                SyntheticThirdAdapter(), Capabilities(public_market_data=True, execution=True, ioc=True))

    def test_opposite_sides_distinct_scopes_and_matching_verified_assets_required(self):
        from funding_collector.domain import Reject, HedgeIntent, Leg, preview_intent
        for a, b, sa, sb in [('alpha', 'alpha', 'buy', 'sell'), ('alpha', 'beta', 'buy', 'buy')]:
            with self.subTest(a=a, b=b, sa=sa, sb=sb), self.assertRaises(Reject):
                universal_plan(a, b, sa, sb)
        registry = fixture_registry()
        original = registry.public('beta').snapshot
        def mismatched(symbol):
            market, book, provenance = original(symbol)
            market.base_asset = 'BTC'
            return market, book, provenance
        registry.public('beta').snapshot = mismatched
        with self.assertRaisesRegex(Reject, 'base asset mismatch'):
            preview_intent(HedgeIntent(Leg('alpha', 'ETH', 'buy', '0'),
                                      Leg('beta', 'ETH', 'sell', '0'), '1'), registry, '200', '10', 101, 5)

    def test_generic_unknown_timeout_never_retries_and_inventory_is_connection_scoped(self):
        from funding_collector.paper import Store, execute
        from funding_collector.domain import Reject
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            with Store(root) as store:
                result = execute(store, store.submit('timeout', {}, universal_plan(), 'timeout'))
                self.assertEqual(result['status'], 'STOPPED_SYNTHETIC')
                self.assertEqual(result['possible_residual_base_bounds'], ['-0.5', '0.0'])
                self.assertEqual(set(store.state['inventory']), {'paper-third-SYNTHETIC-ONLY', 'paper-alpha'})
                self.assertEqual(len([e for e in result['journal'] if e['kind'] == 'fill']), 1)
                with self.assertRaisesRegex(Reject, 'inventory'):
                    store.submit('other', {}, universal_plan('beta', 'alpha'), 'partial')
            durable = Path(root, 'state.json').read_bytes()
            with Store(root) as store:
                execute(store, store.existing('timeout', {}))
            self.assertEqual(Path(root, 'state.json').read_bytes(), durable)

    def test_migration_replace_failure_is_atomic_and_v1_generic_requires_explicit_migration(self):
        import json
        from pathlib import Path
        from unittest.mock import patch
        from funding_collector.paper import Store, atomic_json
        from funding_collector.domain import Reject
        with tempfile.TemporaryDirectory() as root:
            original = {'version': 1, 'mode': 'SYNTHETIC_ONLY', 'actual_accounts_read': False,
                        'accounts': {'core': 'paper-core', 'rh': 'paper-rh'},
                        'inventory': {'core': {}, 'rh': {}}, 'intents': {}}
            atomic_json(Path(root, 'state.json'), original)
            durable = Path(root, 'state.json').read_bytes()
            with Store(root) as store:
                with self.assertRaisesRegex(Reject, 'migrate-store'):
                    store.submit('no-implicit', {}, universal_plan(), 'partial')
                with patch('os.replace', side_effect=OSError('injected atomic replace failure')):
                    with self.assertRaises(OSError):
                        store.migrate()
                self.assertEqual(store.state['version'], 1)
            self.assertEqual(Path(root, 'state.json').read_bytes(), durable)
            self.assertFalse(list(Path(root).glob('*.pending')))


if __name__ == '__main__':
    unittest.main()
