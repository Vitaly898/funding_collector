"""DN v3 synthetic sequencing; no private exchange access."""
import copy
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from test_paper import plan
from funding_collector.paper import Store, execute


def dn_plan(primary='core', side='buy'):
    p = copy.deepcopy(plan())
    hedge = 'rh' if primary == 'core' else 'core'
    p.update(schema_version=3, strategy_mode='DN_FULL_LIMIT_MARKET_V3',
             roles={'primary': primary, 'hedge': hedge}, execution='post-only')
    p['legs'][primary]['side'] = side
    p['legs'][hedge]['side'] = 'sell' if side == 'buy' else 'buy'
    p['legs'][primary].update(order_type='LIMIT', post_only=True, limit_price='100')
    p['legs'][hedge].update(order_type='MARKET')
    return p


class DNFlow(unittest.TestCase):
    def test_simple_ui_parameters_and_dn_preview_limit_selection(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from funding_collector.web import parameters
        from funding_collector.cli import build_preview, parser
        from test_universal import fixture_registry
        values = dict(strategy_mode='DN_FULL_LIMIT_MARKET_V3', primary_venue='rh',
                      direction='short', market='ETH', quantity='1', execution='post-only',
                      core_fee_bps='0', rh_fee_bps='0', max_notional_usd='200',
                      max_roundtrip_cost_usd='10', max_book_age_seconds='5', scenario='partial')
        self.assertEqual(parameters(values), values)
        # CLI exposes bounded DN fields without per-partial controls/timers.
        args = parser().parse_args(['dn-paper', '--intent-id', 'dn', '--primary-venue', 'rh',
                                   '--direction', 'short', '--market', 'ETH', '--quantity', '1',
                                   '--core-fee-bps', '0', '--rh-fee-bps', '0',
                                   '--max-notional-usd', '200', '--max-roundtrip-cost-usd', '10'])
        self.assertEqual(args.strategy_mode, values['strategy_mode'])
        registry = fixture_registry()
        # Same synthetic adapter, explicitly fixed Core/RH identities for this test.
        from funding_collector.adapters import Registry, Connection, Deployment, Capabilities
        r = Registry()
        for v in ('core', 'rh'):
            r.register(Connection(v, Deployment(v, 'lighter'), 'paper-' + v, 'paper-' + v),
                       registry.public('alpha'), Capabilities(public_market_data=True))
        with tempfile.TemporaryDirectory() as root, patch('funding_collector.cli.default_registry', return_value=r), \
                patch('time.time', return_value=101):
            p = build_preview(SimpleNamespace(**values), Path(root))
            self.assertEqual(p['schema_version'], 3)
            self.assertEqual(p['legs']['leg_a']['connection_id'], 'rh')
            self.assertEqual(p['legs']['leg_a']['side'], 'sell')
            self.assertEqual(p['legs']['leg_a']['order_type'], 'LIMIT')
            self.assertEqual(p['legs']['leg_a']['limit_price'], D('101'))
            self.assertTrue(p['legs']['leg_a']['post_only'])
            self.assertEqual(p['legs']['leg_b']['side'], 'buy')
            self.assertEqual(p['legs']['leg_b']['order_type'], 'MARKET')
            with Store(Path(root) / 'store') as store:
                result = execute(store, store.submit('dn', values, p, 'partial'))
                first = next(e for e in result['journal'] if e['kind'] == 'fill')
                self.assertEqual(D(first['vwap']), D('101'))

    def test_market_partial_is_one_order_with_known_residual_not_replenished(self):
        from unittest.mock import patch
        from funding_collector.paper import fill
        def partial(store, intent, event_id, venue, qty):
            return fill(store, intent, event_id, venue, qty / 2 if venue == 'rh' else qty)
        with tempfile.TemporaryDirectory() as root:
            with Store(root) as store, patch('funding_collector.dn.fill', side_effect=partial):
                result = execute(store, store.submit('dn', {}, dn_plan(), 'partial'))
                self.assertEqual(result['status'], 'STOPPED_SYNTHETIC')
                self.assertEqual(D(result['residual_base']), D('0.5'))
                self.assertEqual(result['market_hedge']['status'], 'PARTIAL')
                self.assertEqual(result['pending_unknown_hedge_base'], '0')
            before = Path(root, 'state.json').read_bytes()
            with Store(root) as store:
                execute(store, store.existing('dn', {}))
            self.assertEqual(Path(root, 'state.json').read_bytes(), before)

    def test_pending_market_after_restart_becomes_unknown_without_duplicate(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            with Store(root) as store:
                intent = store.submit('dn', {}, dn_plan(), 'partial')
                save = store.save
                def crash():
                    save()
                    if intent.get('market_hedge'):
                        raise RuntimeError('process lost after write-ahead')
                with patch.object(store, 'save', side_effect=crash), self.assertRaises(RuntimeError):
                    execute(store, intent)
            with Store(root) as store:
                result = execute(store, store.existing('dn', {}))
                self.assertEqual(result['status'], 'STOPPED_SYNTHETIC')
                self.assertEqual(D(result['pending_unknown_hedge_base']), 1)
                self.assertEqual(sum(e['kind'] == 'market_hedge_intent' for e in result['journal']), 1)
                self.assertEqual(result['filled']['rh'], '0')

    def test_incomplete_first_leg_has_no_market_intent(self):
        from unittest.mock import patch
        from funding_collector.paper import fill
        def incomplete(store, intent, event_id, venue, qty):
            if event_id == 'primary-1':
                raise RuntimeError('process lost during accumulation')
            return fill(store, intent, event_id, venue, qty)
        with tempfile.TemporaryDirectory() as root:
            with Store(root) as store, patch('funding_collector.dn.fill', side_effect=incomplete):
                intent = store.submit('dn', {}, dn_plan(), 'partial')
                with self.assertRaises(RuntimeError):
                    execute(store, intent)
                self.assertNotIn('market_hedge', intent)
                self.assertEqual(intent['filled']['rh'], '0')
            with Store(root) as store:
                result = execute(store, store.existing('dn', {}))
                self.assertEqual([e['venue'] for e in result['journal'] if e['kind'] == 'fill'],
                                 ['core', 'core', 'rh'])

    def test_timeout_full_exposure_and_no_blind_retry(self):
        with tempfile.TemporaryDirectory() as root:
            with Store(root) as store:
                result = execute(store, store.submit('dn', {}, dn_plan(), 'timeout'))
                self.assertEqual(result['status'], 'STOPPED_SYNTHETIC')
                self.assertEqual(D(result['filled']['core']), 1)
                self.assertEqual(D(result['pending_unknown_hedge_base']), 1)
                self.assertEqual([D(x) for x in result['possible_residual_base_bounds']], [D(0), D(1)])
            before = Path(root, 'state.json').read_bytes()
            with Store(root) as store:
                execute(store, store.existing('dn', {}))
            self.assertEqual(Path(root, 'state.json').read_bytes(), before)

    def test_full_limit_leg_before_one_exact_market_hedge_both_venues_and_sides(self):
        for venue in ('core', 'rh'):
            for side in ('buy', 'sell'):
                with self.subTest(venue=venue, side=side), tempfile.TemporaryDirectory() as root:
                    with Store(root) as store:
                        p = dn_plan(venue, side)
                        result = execute(store, store.submit('dn', {}, p, 'partial'))
                        primary, hedge = p['roles'].values()
                        fills = [e for e in result['journal'] if e['kind'] == 'fill']
                        self.assertEqual([e['venue'] for e in fills], [primary, primary, hedge])
                        orders = [e for e in result['journal'] if e['kind'] == 'market_hedge_intent']
                        self.assertEqual(len(orders), 1)
                        self.assertEqual(D(orders[0]['quantity_base']), D(result['filled'][primary]))
                        self.assertEqual(orders[0]['side'], p['legs'][hedge]['side'])
                        self.assertEqual(result['strategy_mode'], 'DN_FULL_LIMIT_MARKET_V3')
                        self.assertEqual(result['status'], 'COMPLETED_SYNTHETIC')
                        self.assertEqual(D(result['residual_base']), 0)
                    before = Path(root, 'state.json').read_bytes()
                    with Store(root) as store:
                        execute(store, store.submit('dn', {}, p, 'partial'))
                    self.assertEqual(Path(root, 'state.json').read_bytes(), before)
