"""Bounded DN v3: complete primary LIMIT, then ONE opposite MARKET intent.

Only local synthetic execution. No signer/SDK, account reads or exchange orders.
Original DN uses completed chunks then taker/IOC; v3 deliberately waits for the
entire target as requested. Pending market outcome is never retried blindly.
"""
from decimal import Decimal

from .domain import Reject, number
from .compat import roles
from .paper import event, fill, stop

MODE = 'DN_FULL_LIMIT_MARKET_V3'


def execute_dn(store, intent):
    primary, hedge = roles(intent['preview'])
    target = number(intent['preview']['quantity_base'], True)
    scenario = intent['scenario']
    try:
        for index, chunk in enumerate(intent['chunks']):
            event_id = 'primary-' + str(index)
            if not any(e.get('event_id') == event_id for e in intent['journal']):
                event(intent, 'limit_intent', client_order_id=intent['id'] + '-limit-' + str(index),
                      venue=primary, order_type='LIMIT',
                      post_only=intent['preview']['legs'][primary].get('post_only', True),
                      limit_price=intent['preview']['legs'][primary].get('limit_price'),
                      quantity_base=chunk, simulation='deterministic fill, NOT maker queue')
                fill(store, intent, event_id, primary, number(chunk, True))
        # Fill journal, not cursor/acknowledgement, is the completion authority.
        actual = number(intent['filled'][primary])
        if actual != target:
            raise Reject('primary target is not fully confirmed')
        seen = {e.get('event_id') for e in intent['journal']}
        if 'hedge-full' not in seen:
            # A persisted intent without a confirmed fill has ambiguous outcome.
            if intent.get('market_hedge'):
                return stop(store, intent, 'market outcome UNKNOWN after restart; reconcile, no blind retry',
                            actual - number(intent['filled'][hedge]))
            leg = intent['preview']['legs'][hedge]
            if actual % number(leg['size_step']) or actual < number(leg['min_size']):
                raise Reject('full hedge minimum/precision violation; no silent rounding')
            intent['market_hedge'] = {'client_order_id': intent['id'] + '-market-full',
                                      'order_type': 'MARKET', 'side': leg['side'],
                                      'venue': hedge, 'quantity_base': str(actual), 'status': 'PENDING'}
            event(intent, 'market_hedge_intent', **intent['market_hedge'])
            store.save()  # write-ahead, before the synthetic submission outcome
            if scenario in ('reject', 'outage', 'shutdown'):
                intent['market_hedge']['status'] = 'REJECTED' if scenario == 'reject' else 'NOT_SUBMITTED'
                return stop(store, intent, 'synthetic market hedge ' + scenario + '; no automatic flatten')
            if scenario == 'timeout':
                intent['market_hedge']['status'] = 'UNKNOWN'
                return stop(store, intent, 'synthetic market outcome UNKNOWN; no blind retry', actual)
            if scenario == 'reconnect':
                event(intent, 'reconcile_synthetic', source='durable synthetic fill ledger; NOT exchange account')
            fill(store, intent, 'hedge-full', hedge, actual)
        if number(intent['filled'][hedge]) != actual:
            intent['market_hedge']['status'] = 'PARTIAL'
            return stop(store, intent, 'confirmed market residual; no automatic additional order')
        intent['market_hedge']['status'] = 'FILLED'
        intent['status'] = 'COMPLETED_SYNTHETIC'
        event(intent, 'complete', residual_base=intent['residual_base'], positions_remain_open=True)
        store.save()
        return intent
    except Reject as error:
        return stop(store, intent, 'DN execution rejected: ' + str(error))
    except KeyboardInterrupt:
        unknown = (target - number(intent['filled'][hedge])) if intent.get('market_hedge') else Decimal(0)
        return stop(store, intent, 'operator shutdown; no guaranteed flatten', unknown)
