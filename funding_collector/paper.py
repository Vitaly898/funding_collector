"""Local-only deterministic simulator; durable state is the source of truth.

One locked store models TWO independent EMPTY synthetic accounts, not real accounts.
Each journal/state transition is atomic. No exchange order objects or SDK imports.
"""
from decimal import Decimal, ROUND_DOWN
import fcntl
import json
import os
from pathlib import Path
import re
import time
import uuid

from .domain import Reject, number
from .compat import roles, leg_identity


SCENARIOS = ('partial', 'reject', 'timeout', 'reconnect', 'outage', 'shutdown')


def serial(value):
    return json.loads(json.dumps(value, default=str))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.pending')
    try:
        with temporary.open('x', encoding='utf-8') as out:
            json.dump(value, out, indent=2, default=str)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


class Store:
    def __init__(self, root):
        self.root = Path(root)

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = (self.root / '.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise Reject('store is locked by another process') from None
        try:
            path = self.root / 'state.json'
            self.state = json.loads(path.read_text()) if path.exists() else {
                'version': 2, 'mode': 'SYNTHETIC_ONLY',
                'actual_accounts_read': False,
                'accounts': {}, 'inventory': {}, 'intents': {}}
            if self.state.get('version') not in (1, 2) or self.state.get('mode') != 'SYNTHETIC_ONLY':
                raise Reject('unknown store version or mode')
            return self
        except BaseException:
            self.lock.close()
            raise

    def __exit__(self, *args):
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()

    def migrate(self):
        """Explicit atomic v1→v2 header migration; never rename/reinterpret history.

        v1 intents keep their original payload, role and inventory namespace.
        read/status/retry never migrate automatically.
        """
        if self.state['version'] == 1:
            migrated = dict(self.state, version=2, migration={
                'from_version': 1, 'legacy_inventory_and_intents_preserved': True})
            atomic_json(self.root / 'state.json', migrated)
            self.state = migrated

    def save(self):
        atomic_json(self.root / 'state.json', self.state)
        # These are projections; a crash cannot cause a fill replay from a partial journal.
        for identifier, intent in self.state['intents'].items():
            directory = self.root / identifier
            atomic_json(directory / 'preview.json', intent['preview'])
            atomic_json(directory / 'result.json', intent)
            text = ''.join(json.dumps(e) + '\n' for e in intent['journal'])
            (directory / 'synthetic-journal.jsonl').write_text(text, encoding='utf-8')

    def existing(self, identifier, payload):
        intent = self.state['intents'].get(identifier)
        if intent and intent['payload'] != serial(payload):
            raise Reject('idempotency key reused with different intent/risk/scenario')
        return intent

    def submit(self, identifier, payload, preview, scenario):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', identifier):
            raise Reject('invalid intent id')
        if scenario not in SCENARIOS:
            raise Reject('unknown synthetic scenario')
        old = self.existing(identifier, payload)
        if old:
            if old['scenario'] != scenario:
                raise Reject('idempotency scenario conflict')
            return old
        if any(number(q) != 0 for inventory in self.state['inventory'].values() for q in inventory.values()):
            raise Reject('nonempty synthetic account inventory: new entry refused')
        if any(i['status'] == 'RUNNING_SYNTHETIC' for i in self.state['intents'].values()):
            raise Reject('existing working synthetic intent; resume it first')
        p = serial(preview)
        strategy = p.get('strategy_mode', 'LEGACY_PER_PARTIAL_V1_V2')
        if strategy not in ('LEGACY_PER_PARTIAL_V1_V2', 'DN_FULL_LIMIT_MARKET_V3'):
            raise Reject('unknown execution strategy mode')
        if p.get('schema_version') == 3:
            if strategy != 'DN_FULL_LIMIT_MARKET_V3' or self.state['version'] == 1:
                raise Reject('DN v3 requires explicit strategy mode and a v2 store; use a separate new store')
        elif strategy == 'DN_FULL_LIMIT_MARKET_V3':
            raise Reject('DN strategy requires schema version 3')
        if p.get('schema_version') == 2 and self.state['version'] == 1:
            raise Reject('v1 store: use explicit migrate-store before new generic intents')
        primary, hedge = roles(p)
        if set(p['legs']) != {primary, hedge} or primary == hedge:
            raise Reject('exactly two distinct explicit leg roles required')
        bindings = {}
        for key in (primary, hedge):
            inventory_key, asset, account = leg_identity(p, key)
            leg = p['legs'][key]
            identity = ({field: leg[field] for field in
                         ('connection_id', 'venue', 'deployment_id', 'account_identity')}
                        if 'connection_id' in leg else key)
            saved = self.state['accounts'].get(inventory_key)
            if saved is not None and saved != identity and not (isinstance(identity, str) and saved == account):
                raise Reject('inventory connection identity conflict')
            bindings[inventory_key] = identity
        qty = number(p['quantity_base'], True)
        # Least common base increment, not max(step): 0.02 and 0.03 require 0.06.
        from math import lcm
        ratios = [number(leg['size_step'], True).as_integer_ratio() for leg in p['legs'].values()]
        denominator = lcm(*(d for _, d in ratios))
        step = Decimal(lcm(*(n * (denominator // d) for n, d in ratios))) / denominator
        if qty % step:
            raise Reject('quantity incompatible with both base increments')
        half = (qty / 2 / step).to_integral_value(rounding=ROUND_DOWN) * step
        chunks = [half, qty - half] if half else [qty]
        intent = {'id': identifier, 'payload': serial(payload), 'preview': p, 'scenario': scenario,
                  'strategy_mode': strategy,
                  'status': 'RUNNING_SYNTHETIC', 'journal': [], 'chunks': [str(q) for q in chunks],
                  'filled': {key: '0' for key in p['legs']}, 'cursor': 0,
                  'residual_base': '0', 'pending_unknown_hedge_base': '0',
                  'initial_inventory': {leg_identity(p, key)[2]: '0' for key in p['legs']},
                  'inventory_assumption': 'EMPTY LOCAL SIMULATION; NOT actual positions or working orders'}
        self.state['accounts'].update(bindings)
        for inventory_key in bindings:
            self.state['inventory'].setdefault(inventory_key, {})
        self.state['intents'][identifier] = intent
        self.save()
        return intent


def event(intent, kind, **values):
    intent['journal'].append(dict(sequence=len(intent['journal']), kind=kind,
                                  synthetic=True, recorded_at_unix_s=time.time(), **values))


def fill(store, intent, event_id, venue, qty):
    qty = number(qty, True)
    if any(e.get('event_id') == event_id for e in intent['journal']):
        raise Reject('duplicate fill event')
    if intent['status'] != 'RUNNING_SYNTHETIC':
        raise Reject('intent is stopped or complete')
    leg = intent['preview']['legs'][venue]
    already = number(intent['filled'][venue])
    if already + qty > number(intent['preview']['quantity_base']):
        raise Reject('oversize fill rejected')
    # Consume the frozen executable ladder, without reusing depth across partials.
    skip, left, notional = already, qty, Decimal(0)
    for level in leg['fills']:
        available = number(level['quantity'])
        if skip >= available:
            skip -= available
            continue
        take = min(left, available - skip)
        notional += take * number(level['price'])
        left -= take
        skip = Decimal(0)
        if left == 0:
            break
    if left:
        raise Reject('oversize relative to frozen visible depth')
    # A hedge partial is a separate submitted order: validate its actual slice.
    if venue == roles(intent['preview'])[1] and notional < number(leg['min_notional']):
        raise Reject('partial cannot satisfy hedge minimum notional')
    sign = 1 if leg['side'] == 'buy' else -1
    inventory_key, symbol, account = leg_identity(intent['preview'], venue)
    inventory = store.state['inventory'][inventory_key]
    inventory[symbol] = str(number(inventory.get(symbol, '0')) + sign * qty)
    intent['filled'][venue] = str(already + qty)
    intent['residual_base'] = str(sum(number(store.state['inventory'][leg_identity(intent['preview'], v)[0]].get(symbol, '0')) for v in intent['preview']['legs']))
    fee = notional * number(intent['preview']['fees_bps_assumed_not_account_verified'][venue]) / 10000
    event(intent, 'fill', event_id=event_id, venue=venue, account=account,
          connection_id=leg.get('connection_id', venue),
          side=leg['side'], quantity=str(qty), vwap=str(notional / qty), notional_usd=str(notional),
          fee_usd=str(fee), residual_base=intent['residual_base'])
    store.save()


def stop(store, intent, reason, unknown=Decimal(0)):
    intent['status'] = 'STOPPED_SYNTHETIC'
    intent['stop_reason'] = reason
    intent['flatten_guaranteed'] = False
    intent['pending_unknown_hedge_base'] = str(unknown)
    residual = number(intent['residual_base'])
    # An unacknowledged hedge may have filled; never blindly retry an unknown order.
    direction = 1 if intent['preview']['legs'][roles(intent['preview'])[1]]['side'] == 'buy' else -1
    bounds = sorted([residual, residual + direction * unknown])
    intent['possible_residual_base_bounds'] = [str(q) for q in bounds]
    event(intent, 'stop', reason=reason, residual_base=str(residual),
          unknown_hedge_base=str(unknown), flatten_guaranteed=False)
    store.save()
    return intent


def execute(store, intent):
    if intent['status'] != 'RUNNING_SYNTHETIC':
        store.save()  # reconstruct exported artifacts after a crash
        return intent
    if intent.get('strategy_mode') == 'DN_FULL_LIMIT_MARKET_V3':
        from .dn import execute_dn
        return execute_dn(store, intent)
    if intent.get('strategy_mode', 'LEGACY_PER_PARTIAL_V1_V2') != 'LEGACY_PER_PARTIAL_V1_V2':
        raise Reject('unknown execution strategy mode')
    primary, hedge = roles(intent['preview'])
    scenario = intent['scenario']
    policy = intent['payload']
    cap = number(policy.get('max_unhedged_base', intent['preview']['quantity_base']), True)
    delay_cap = number(policy.get('max_hedge_delay_ms', '1000'), True)
    try:
        for index in range(intent['cursor'], len(intent['chunks'])):
            qty = number(intent['chunks'][index], True)
            primary_id, hedge_id = 'primary-' + str(index), 'hedge-' + str(index)
            seen = {e.get('event_id') for e in intent['journal']}
            if primary_id not in seen:
                if qty > cap:
                    return stop(store, intent, 'primary partial exceeds max unhedged base')
                fill(store, intent, primary_id, primary, qty)
            if hedge_id not in seen:
                if scenario in ('reject', 'outage', 'shutdown'):
                    return stop(store, intent, 'synthetic hedge ' + scenario + '; no automatic flatten')
                if scenario == 'timeout':
                    return stop(store, intent, 'synthetic hedge timeout: outcome UNKNOWN; no blind retry', qty)
                # Virtual latency only: no assertion about real-world hedge speed.
                simulated_delay_ms = Decimal('1') if scenario != 'reconnect' else Decimal('5')
                if scenario == 'reconnect' and not any(e.get('reconcile_index') == index for e in intent['journal']):
                    event(intent, 'disconnect_synthetic', index=index)
                    event(intent, 'reconcile_synthetic', reconcile_index=index,
                          source='durable local synthetic fill ledger ONLY; no real account query')
                    store.save()
                if simulated_delay_ms > delay_cap:
                    return stop(store, intent, 'synthetic hedge delay risk limit exceeded')
                leg = intent['preview']['legs'][roles(intent['preview'])[1]]
                if (qty % number(leg['size_step']) or qty < number(leg['min_size'])):
                    return stop(store, intent, 'partial cannot satisfy hedge minimum/precision')
                fill(store, intent, hedge_id, hedge, qty)
                event(intent, 'hedge_latency_synthetic', virtual_delay_ms=str(simulated_delay_ms), index=index)
            intent['cursor'] = index + 1
            store.save()
        intent['status'] = 'COMPLETED_SYNTHETIC'
        event(intent, 'complete', residual_base=intent['residual_base'], positions_remain_open=True)
        store.save()
        return intent
    except Reject as error:
        return stop(store, intent, 'execution rejected: ' + str(error))
    except KeyboardInterrupt:
        return stop(store, intent, 'operator shutdown; no guaranteed flatten')
