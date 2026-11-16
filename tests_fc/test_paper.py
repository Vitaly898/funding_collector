"""Deterministic synthetic hedge execution tests, no exchange account access."""
import json
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal as D

from test_domain import metadata, book


def plan():
    from funding_collector.domain import Market, preview
    return preview({v: Market(metadata()) for v in ('core', 'rh')},
                   {v: book() for v in ('core', 'rh')},
                   'ETH', 'core-long', '1', '200', '10', '0', '0', 101, 5)


class Paper(unittest.TestCase):
    def test_each_partial_hedged_before_next_primary_and_persistent_retry(self):
        from funding_collector.paper import Store, execute
        with tempfile.TemporaryDirectory() as root:
            with Store(root) as store:
                intent = store.submit('test-1', {'quantity': '1'}, plan(), 'partial')
                result = execute(store, intent)
                fills = [e for e in result['journal'] if e['kind'] == 'fill']
                self.assertEqual([e['venue'] for e in fills], ['core', 'rh', 'core', 'rh'])
                self.assertEqual([D(e['quantity']) for e in fills], [D('0.5')] * 4)
                self.assertEqual(D(result['residual_base']), D('0'))
                self.assertEqual(result['status'], 'COMPLETED_SYNTHETIC')
                count = len(result['journal'])
            with Store(root) as store:
                replay = store.submit('test-1', {'quantity': '1'}, plan(), 'partial')
                execute(store, replay)
                self.assertEqual(len(replay['journal']), count)
                self.assertEqual(D(store.state['inventory']['core']['ETH']), D('1'))
                self.assertEqual(D(store.state['inventory']['rh']['ETH']), D('-1'))
            self.assertTrue((Path(root) / 'state.json').exists())
            self.assertEqual(len((Path(root) / 'test-1' / 'synthetic-journal.jsonl').read_text().splitlines()), count)

    def test_failures_stop_with_residual_and_no_fake_flatten(self):
        from funding_collector.paper import Store, execute
        for scenario in ('reject', 'timeout', 'outage', 'shutdown'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as root:
                with Store(root) as store:
                    intent = store.submit('x', {}, plan(), scenario)
                    execute(store, intent)
                    self.assertEqual(intent['status'], 'STOPPED_SYNTHETIC')
                    self.assertEqual(D(intent['residual_base']), D('0.5'))
                    self.assertEqual(len([e for e in intent['journal'] if e['kind'] == 'fill']), 1)
                    self.assertFalse(intent['flatten_guaranteed'])
                    if scenario == 'timeout':
                        self.assertEqual(D(intent['pending_unknown_hedge_base']), D('0.5'))
                    size = len(intent['journal'])
                    execute(store, intent)
                    self.assertEqual(len(intent['journal']), size)

    def test_reconnect_reconciles_synthetic_fills_without_replay(self):
        from funding_collector.paper import Store, execute
        with tempfile.TemporaryDirectory() as root:
            with Store(root) as store:
                intent = store.submit('x', {}, plan(), 'reconnect')
                execute(store, intent)
                self.assertEqual(intent['status'], 'COMPLETED_SYNTHETIC')
                self.assertEqual(len([e for e in intent['journal'] if e['kind'] == 'fill']), 4)
                self.assertIn('reconcile_synthetic', [e['kind'] for e in intent['journal']])

    def test_crash_resume_hedges_pending_partial_before_any_new_primary(self):
        from funding_collector.paper import Store, execute, fill
        with tempfile.TemporaryDirectory() as root:
            with Store(root) as store:
                intent = store.submit('x', {}, plan(), 'partial')
                fill(store, intent, 'primary-0', 'core', D('0.5'))
            with Store(root) as store:
                intent = store.existing('x', {})
                execute(store, intent)
                self.assertEqual([e['venue'] for e in intent['journal'] if e['kind'] == 'fill'],
                                 ['core', 'rh', 'core', 'rh'])

    def test_partial_below_hedge_minimum_stops_with_actual_synthetic_residual(self):
        from funding_collector.paper import Store, execute
        p = plan()
        p['legs']['rh']['min_size'] = '0.6'
        with tempfile.TemporaryDirectory() as root:
            with Store(root) as store:
                intent = store.submit('x', {}, p, 'partial')
                execute(store, intent)
                self.assertEqual(intent['status'], 'STOPPED_SYNTHETIC')
                self.assertEqual(D(intent['residual_base']), D('0.5'))
                self.assertIn('hedge minimum', intent['stop_reason'])

    def test_residual_cap_prevents_primary_and_fill_delay_stops_hedge(self):
        from funding_collector.paper import Store, execute
        with tempfile.TemporaryDirectory() as root:
            with Store(root) as store:
                intent = store.submit('x', {'max_unhedged_base': '0.1'}, plan(), 'partial')
                execute(store, intent)
                self.assertEqual(D(intent['residual_base']), D('0'))
                self.assertEqual(intent['status'], 'STOPPED_SYNTHETIC')
        with tempfile.TemporaryDirectory() as root:
            with Store(root) as store:
                intent = store.submit('x', {'max_hedge_delay_ms': '0.5'}, plan(), 'partial')
                execute(store, intent)
                self.assertEqual(intent['status'], 'STOPPED_SYNTHETIC')
                self.assertEqual(D(intent['residual_base']), D('0.5'))

    def test_internal_hedge_rejection_is_persisted_as_failure_stop(self):
        from funding_collector.paper import Store, execute, fill
        from funding_collector.domain import Reject
        from unittest.mock import patch
        def reject_hedge(store, intent, event_id, venue, qty):
            if venue == 'rh':
                raise Reject('synthetic adapter rejection')
            return fill(store, intent, event_id, venue, qty)
        with tempfile.TemporaryDirectory() as root:
            with Store(root) as store:
                intent = store.submit('x', {}, plan(), 'partial')
                with patch('funding_collector.paper.fill', side_effect=reject_hedge):
                    execute(store, intent)
                self.assertEqual(intent['status'], 'STOPPED_SYNTHETIC')
                self.assertEqual(D(intent['residual_base']), D('0.5'))
                self.assertIn('synthetic adapter rejection', intent['stop_reason'])

    def test_keyboard_interrupt_records_stop_without_flatten(self):
        from funding_collector.paper import Store, execute, fill
        from unittest.mock import patch
        def interrupt_hedge(store, intent, event_id, venue, qty):
            if venue == 'rh':
                raise KeyboardInterrupt
            return fill(store, intent, event_id, venue, qty)
        with tempfile.TemporaryDirectory() as root:
            with Store(root) as store:
                intent = store.submit('x', {}, plan(), 'partial')
                with patch('funding_collector.paper.fill', side_effect=interrupt_hedge):
                    execute(store, intent)
                self.assertEqual(intent['status'], 'STOPPED_SYNTHETIC')
                self.assertEqual(D(intent['residual_base']), D('0.5'))
                self.assertFalse(intent['flatten_guaranteed'])

    def test_store_lock_refuses_concurrent_writer(self):
        from funding_collector.paper import Store
        from funding_collector.domain import Reject
        with tempfile.TemporaryDirectory() as root:
            with Store(root):
                with self.assertRaisesRegex(Reject, 'locked'):
                    with Store(root):
                        pass

    def test_duplicate_conflict_nonempty_inventory_and_oversize_rejected(self):
        from funding_collector.paper import Store, execute, fill
        from funding_collector.domain import Reject
        with tempfile.TemporaryDirectory() as root:
            with Store(root) as store:
                intent = store.submit('x', {'quantity': '1'}, plan(), 'partial')
                with self.assertRaisesRegex(Reject, 'idempotency'):
                    store.submit('x', {'quantity': '2'}, plan(), 'partial')
                with self.assertRaisesRegex(Reject, 'oversize'):
                    fill(store, intent, 'bad', 'core', D('2'))
                execute(store, intent)
                with self.assertRaisesRegex(Reject, 'inventory'):
                    store.submit('other', {'quantity': '1'}, plan(), 'partial')
                with self.assertRaisesRegex(Reject, 'duplicate'):
                    fill(store, intent, 'primary-0', 'core', D('0.50'))
