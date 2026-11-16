"""Universal credential-free intent CLI. Public reads + LOCAL simulation only."""
import argparse
import json
from pathlib import Path
import re
import sys
import time
import uuid

from .domain import HedgeIntent, Leg, Reject, number, preview_intent
from .paper import SCENARIOS, Store, atomic_json, execute
from .lighter import default_registry
from . import legacy_cli

LEG_FIELDS = tuple('leg_' + leg + '_' + field for leg in ('a', 'b')
                   for field in ('connection', 'instrument', 'side', 'fee_bps'))
RISK_FIELDS = ('quantity', 'max_notional_usd', 'max_roundtrip_cost_usd',
               'max_book_age_seconds', 'max_unhedged_base', 'max_hedge_delay_ms')
DN_FIELDS = ('strategy_mode', 'primary_venue', 'direction', 'market', 'quantity', 'execution',
             'core_fee_bps', 'rh_fee_bps', 'max_notional_usd', 'max_roundtrip_cost_usd',
             'max_book_age_seconds', 'scenario')
DN_PAIR_FIELDS = tuple(f for f in DN_FIELDS if f not in ('core_fee_bps', 'rh_fee_bps')) + (
    'hedge_venue', 'hedge_market', 'primary_fee_bps', 'hedge_fee_bps')


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse otherwise echoes arbitrary rejected argv values (possibly secrets).
        self.print_usage(sys.stderr)
        self.exit(2, 'Invalid arguments; use --help. Secret arguments are never accepted.\n')


def parser():
    cli = SafeParser(description='Universal two-leg hedge: TRADING DISABLED; public data and SYNTHETIC execution only')
    commands = cli.add_subparsers(dest='command', required=True)
    server = commands.add_parser('serve', help='loopback UI; public quotes + SYNTHETIC fills')
    server.add_argument('--store', default='local_runs/ui-dn-v3')
    server.add_argument('--host', choices=('127.0.0.1',), default='127.0.0.1')
    server.add_argument('--port', type=int, default=8765)
    server.add_argument('--live-root', type=Path, help='opt-in sanitized read-only live ledger projection; never activation')
    for name in ('connections', 'migrate-store'):
        sub = commands.add_parser(name)
        sub.add_argument('--store', default='local_runs')
    for name in ('preview', 'paper', 'status', 'resume'):
        sub = commands.add_parser(name)
        sub.add_argument('--store', default='local_runs', help='persistent local simulated account ledger')
        sub.add_argument('--intent-id', required=True)
        if name in ('status', 'resume'):
            continue
        legacy_cli.configure(sub)
        for leg in ('a', 'b'):
            sub.add_argument('--leg-' + leg + '-connection', help='registered connection ID, never URL')
            sub.add_argument('--leg-' + leg + '-instrument', help='exact venue instrument symbol')
            sub.add_argument('--leg-' + leg + '-side', choices=('buy', 'sell'))
            sub.add_argument('--leg-' + leg + '-fee-bps', help='explicit assumed fee, not account verified')
        sub.add_argument('--quantity', required=True, help='verified base units on EACH leg')
        sub.add_argument('--max-notional-usd', required=True)
        sub.add_argument('--max-roundtrip-cost-usd', required=True)
        sub.add_argument('--max-book-age-seconds', default='5')
        sub.add_argument('--max-unhedged-base', required=True)
        sub.add_argument('--max-hedge-delay-ms', required=True, help='virtual simulation risk budget')
        sub.add_argument('--scenario', choices=SCENARIOS, default='partial')
        sub.add_argument('--assume-empty-simulated', action='store_true')
    for name in ('dn-preview', 'dn-paper'):
        sub = commands.add_parser(name, help='DN v3: full LIMIT target then one MARKET hedge; synthetic only')
        sub.set_defaults(strategy_mode='DN_FULL_LIMIT_MARKET_V3')
        sub.add_argument('--store', default='local_runs/dn-v3')
        sub.add_argument('--intent-id', required=True)
        sub.add_argument('--primary-venue', choices=('core', 'rh', 'extended', 'entropy', 'evedex', 'hyperliquid'), required=True)
        sub.add_argument('--hedge-venue', choices=('core', 'rh', 'extended', 'entropy', 'evedex', 'hyperliquid'))
        sub.add_argument('--hedge-market')
        sub.add_argument('--primary-fee-bps')
        sub.add_argument('--hedge-fee-bps')
        sub.add_argument('--direction', choices=('long', 'short'), required=True)
        sub.add_argument('--execution', choices=('post-only', 'limit'), default='post-only')
        for field in ('market', 'quantity', 'max_notional_usd', 'max_roundtrip_cost_usd'):
            sub.add_argument('--' + field.replace('_', '-'), required=True)
        sub.add_argument('--core-fee-bps')
        sub.add_argument('--rh-fee-bps')
        sub.add_argument('--max-book-age-seconds', default='30')
        sub.add_argument('--scenario', choices=SCENARIOS, default='partial')
        sub.add_argument('--assume-empty-simulated', action='store_true')
    from .supervised import configure
    configure(commands)
    return cli


def explicit_intent(values):
    return HedgeIntent(*(Leg(*(values['leg_' + leg + '_' + field]
                             for field in ('connection', 'instrument', 'side', 'fee_bps')))
                         for leg in ('a', 'b')), values['quantity'])


def validate_values(values):
    if values.get('strategy_mode') is not None:
        paired = values.get('hedge_venue') is not None
        if (values['strategy_mode'] != 'DN_FULL_LIMIT_MARKET_V3'
                or values.get('primary_venue') not in (('core', 'rh', 'extended', 'entropy', 'evedex', 'hyperliquid') if paired else ('core', 'rh'))
                or values.get('direction') not in ('long', 'short')
                or values.get('execution') not in ('post-only', 'limit')):
            raise Reject('invalid DN strategy/venue/direction/execution selection')
        if not re.fullmatch(r'(?:io:)?[A-Za-z0-9_.-]{1,30}', values.get('market', '')):
            raise Reject('invalid exact instrument symbol')
        for key in ('quantity', 'max_notional_usd', 'max_roundtrip_cost_usd', 'max_book_age_seconds'):
            number(values[key], True)
        if paired and (values['hedge_venue'] not in ('core', 'rh', 'extended', 'entropy', 'evedex', 'hyperliquid')
                       or values['hedge_venue'] == values['primary_venue']
                       or not re.fullmatch(r'(?:io:)?[A-Za-z0-9_.-]{1,30}', values.get('hedge_market', ''))
                       or any(values.get(k) is not None for k in ('core_fee_bps', 'rh_fee_bps'))):
            raise Reject('invalid explicit DN pair or mixed legacy fees')
        for key in (('primary_fee_bps', 'hedge_fee_bps') if paired else ('core_fee_bps', 'rh_fee_bps')):
            if number(values[key]) < 0:
                raise Reject('fee cannot be negative')
        return True
    for key in RISK_FIELDS:
        number(values[key], True)
    explicit = any(values.get(key) is not None for key in LEG_FIELDS)
    legacy = any(values.get(key) is not None for key in legacy_cli.LEGACY_FIELDS)
    if explicit == legacy:
        raise Reject('use exactly one complete explicit leg pair OR the v1 CLI fields; do not mix')
    needed = LEG_FIELDS if explicit else legacy_cli.LEGACY_FIELDS
    if any(values.get(key) is None for key in needed):
        raise Reject('incomplete leg/fee specification')
    for key in needed:
        if key.endswith('fee_bps') and number(values[key]) < 0:
            raise Reject('fee cannot be negative')
    if explicit:
        # Registry is fixed; constructing it performs no network reads.
        explicit_intent(values).validate(default_registry(Path(values.get('store', 'local_runs')) / 'public'))
    return explicit


def build_preview(args, artifacts):
    if getattr(args, 'strategy_mode', None) == 'DN_FULL_LIMIT_MARKET_V3':
        validate_values(vars(args))
        primary = args.primary_venue
        hedge = getattr(args, 'hedge_venue', None) or ('rh' if primary == 'core' else 'core')
        side = 'buy' if args.direction == 'long' else 'sell'
        paired = getattr(args, 'hedge_venue', None) is not None
        intent = HedgeIntent(Leg(primary, args.market, side, args.primary_fee_bps if paired else getattr(args, primary + '_fee_bps')),
                             Leg(hedge, args.hedge_market if paired else args.market, 'sell' if side == 'buy' else 'buy',
                                 args.hedge_fee_bps if paired else getattr(args, hedge + '_fee_bps')), args.quantity)
        p = preview_intent(intent, default_registry(artifacts / 'public'), args.max_notional_usd,
                           args.max_roundtrip_cost_usd, time.time, float(args.max_book_age_seconds))
        p.update(schema_version=3, strategy_mode=args.strategy_mode, execution=args.execution)
        leg = p['legs']['leg_a']
        price = (p['exit_estimate']['leg_a']['fills'][0]['price']
                 if args.execution == 'post-only' else leg['worst_price'])
        if number(args.quantity) * price > number(args.max_notional_usd):
            raise Reject('primary limit-price notional exceeds cap')
        leg.update(order_type='LIMIT', post_only=args.execution == 'post-only', limit_price=price)
        # These are synthetic fills at the selected limit, not a claim of queue priority.
        leg['fills'] = [{'price': price, 'quantity': number(args.quantity)}]
        p['legs']['leg_b']['order_type'] = 'MARKET'
        p['warning'] = ('DN v3: full LIMIT target before one MARKET hedge. Accumulation is unhedged; '
                        'synthetic fills are NOT a maker-queue model. Cost figures remain static all-taker benchmarks.')
        p['created_at_unix_s'] = time.time()
        atomic_json(artifacts / 'preview.json', p)
        return p
    if getattr(args, 'direction', None) is not None:
        return legacy_cli.build_legacy_preview(args, artifacts)
    started = time.time()
    result = preview_intent(explicit_intent(vars(args)), default_registry(artifacts / 'public'),
                            args.max_notional_usd, args.max_roundtrip_cost_usd,
                            time.time, float(args.max_book_age_seconds))
    result['public_snapshot_started_at_unix_s'] = started
    result['risk'] = {key: getattr(args, key) for key in RISK_FIELDS if key != 'quantity'}
    result['created_at_unix_s'] = time.time()
    result['artifacts'] = str(artifacts.resolve())
    atomic_json(artifacts / 'preview.json', result)
    return result


def summary(intent, root):
    return {'mode': 'SYNTHETIC_ONLY_TRADING_DISABLED', 'id': intent['id'],
            'strategy_mode': intent.get('strategy_mode', 'LEGACY_PER_PARTIAL_V1_V2'),
            'market_hedge': intent.get('market_hedge'),
            'status': intent['status'], 'scenario': intent['scenario'],
            'filled_base': intent['filled'], 'residual_base': intent['residual_base'],
            'pending_unknown_hedge_base': intent['pending_unknown_hedge_base'],
            'possible_residual_base_bounds': intent.get('possible_residual_base_bounds'),
            'stop_reason': intent.get('stop_reason'),
            'synthetic_fill_count': sum(e['kind'] == 'fill' for e in intent['journal']),
            'actual_accounts_read': False, 'flatten_guaranteed': False,
            'preview': intent['preview'], 'state_path': str((root / 'state.json').resolve()),
            'result_path': str((root / intent['id'] / 'result.json').resolve())}


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == 'live':
        from .supervised import main as live_main
        return live_main(args)
    if args.command in ('dn-preview', 'dn-paper'):
        args.command = args.command.removeprefix('dn-')
    artifacts = None
    try:
        if args.command == 'serve':
            from .web import serve
            return serve(args.store, args.host, args.port, live_root=args.live_root)
        if args.command == 'connections':
            print(json.dumps(default_registry(Path(args.store) / 'public').describe(), indent=2))
            return 0
        with Store(args.store) as store:
            if args.command == 'migrate-store':
                store.migrate()
                print(json.dumps({'version': store.state['version'], 'mode': 'SYNTHETIC_ONLY'}))
                return 0
            if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', args.intent_id):
                raise Reject('invalid intent id')
            if args.command in ('status', 'resume'):
                intent = store.state['intents'].get(args.intent_id)
                if not intent:
                    raise Reject('unknown intent')
                if args.command == 'resume':
                    execute(store, intent)  # offline saved simulation only
                output = summary(intent, store.root)
            else:
                if args.command == 'paper' and not args.assume_empty_simulated:
                    raise Reject('paper requires --assume-empty-simulated; real inventory NOT read')
                values = vars(args)
                # Remove only unused option-family None values. Legacy payload bytes/strings
                # and every supplied generic field remain exact for idempotency.
                validate_values(values)
                payload = {k: v for k, v in values.items()
                           if k not in ('store', 'command', 'intent_id') and v is not None}
                old = store.existing(args.intent_id, payload)
                if old:
                    if args.command != 'paper':
                        raise Reject('intent already submitted; use status, resume or exact paper retry')
                    output = summary(execute(store, old), store.root)
                else:
                    artifacts = store.root / args.intent_id / 'attempts' / uuid.uuid4().hex
                    artifacts.mkdir(parents=True, exist_ok=False)
                    p = build_preview(args, artifacts)
                    if args.command == 'paper':
                        output = summary(execute(store, store.submit(args.intent_id, payload, p, args.scenario)), store.root)
                    else:
                        output = p
            print(json.dumps(output, indent=2, default=str))
            return 3 if output.get('status') == 'STOPPED_SYNTHETIC' else 0
    except (Reject, OSError, ValueError, KeyError, TypeError) as error:
        result = {'mode': 'TRADING_DISABLED', 'status': 'REJECTED',
                  'error': str(error), 'recorded_at_unix_s': time.time()}
        if artifacts:
            atomic_json(artifacts / 'error.json', result)
        print(json.dumps(result), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('{"status":"INTERRUPTED","trading_enabled":false,"flatten_guaranteed":false}', file=sys.stderr)
        return 130
