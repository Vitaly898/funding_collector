"""Version-1 Core/RH API and durable-ledger semantics, kept explicit.

Do NOT rename version-1 inventory/account/event keys or normalize payload strings.
Existing completed retries must remain byte-identical and network-free.
"""
from decimal import Decimal
from .domain import Reject, number


def roles(preview):
    if 'roles' in preview:
        return preview['roles']['primary'], preview['roles']['hedge']
    if set(preview['legs']) != {'core', 'rh'}:
        raise Reject('unrecognized legacy legs; explicit roles required')
    return 'core', 'rh'


def leg_identity(preview, key):
    leg = preview['legs'][key]
    if 'connection_id' in leg:
        return leg['inventory_key'], leg['base_asset'], leg['account']
    if key not in ('core', 'rh'):
        raise Reject('unrecognized legacy inventory identity')
    return key, preview['symbol'], 'paper-' + key


def preview(markets, books, symbol, direction, quantity, max_notional, max_cost,
            core_fee_bps, rh_fee_bps, now, max_age):
    """Static all-taker roundtrip estimate, not funding/profit or fill promise."""
    qty = number(quantity, True)
    cap, cost_cap = number(max_notional, True), number(max_cost, True)
    fees = {'core': number(core_fee_bps), 'rh': number(rh_fee_bps)}
    if any(f < 0 for f in fees.values()) or direction not in ('core-long', 'rh-long'):
        raise Reject('invalid fee or direction')
    legs, exits, fee_entry, fee_exit = {}, {}, Decimal(0), Decimal(0)
    signed_entry, signed_exit = Decimal(0), Decimal(0)
    for venue in ('core', 'rh'):
        market = markets[venue]
        if market.symbol != symbol:
            raise Reject('market identity mismatch')
        side = 'buy' if (venue == 'core') == (direction == 'core-long') else 'sell'
        close_side = 'sell' if side == 'buy' else 'buy'
        entry = books[venue].walk(side, qty, now, max_age)
        close = books[venue].walk(close_side, qty, now, max_age)
        for estimate in (entry, close):
            market.validate(qty, estimate['worst_price'])
            # Validate every visible price consumed, not a rounded VWAP.
            for fill in estimate['fills']:
                if fill['price'] % market.price_step:
                    raise Reject('book price precision violation')
            if max(estimate['notional'], qty * estimate['worst_price']) > cap:
                raise Reject('max notional per leg exceeded')
        entry = dict(entry, side=side, market_id=market.market_id,
                     size_step=market.size_step, min_size=market.min_size,
                     min_notional=market.min_quote,
                     account='paper-' + venue, inventory_source='local synthetic only; actual account NOT read')
        legs[venue], exits[venue] = entry, close
        fee_entry += entry['notional'] * fees[venue] / 10000
        fee_exit += close['notional'] * fees[venue] / 10000
        signed_entry += entry['notional'] * (1 if side == 'buy' else -1)
        signed_exit += close['notional'] * (1 if close_side == 'buy' else -1)
    total = signed_entry + signed_exit + fee_entry + fee_exit
    if total > cost_cap:
        raise Reject('roundtrip cost cap exceeded')
    return {'mode': 'PUBLIC_PREVIEW_TRADING_DISABLED', 'symbol': symbol,
            'quantity_base': qty, 'direction': direction, 'legs': legs, 'exit_estimate': exits,
            'entry_cash_cost_usd': signed_entry, 'entry_fee_usd': fee_entry,
            'exit_fee_usd': fee_exit, 'roundtrip_cost_usd': total,
            'fees_bps_assumed_not_account_verified': fees,
            'funding': 'NOT_FETCHED_OR_INCLUDED', 'max_notional_per_leg_usd': cap,
            'max_roundtrip_cost_usd': cost_cap,
            'warning': 'Static snapshots; no guaranteed fills, exit, flatten or profitability. No leverage/collateral/account checks.'}
