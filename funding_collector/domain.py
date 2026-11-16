"""Exact-decimal executable book mechanics. No trading dependencies."""
from decimal import Decimal, InvalidOperation


class Reject(ValueError):
    """Fail-closed input, market data or risk rejection."""


def number(value, positive=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise Reject('invalid decimal') from None
    if not result.is_finite() or (positive and result <= 0):
        raise Reject('nonfinite or nonpositive decimal')
    return result


class Instrument:
    """Verified normalized units. Only linear, unit-base, USD quote supported.

    No inference of inverse/quanto/lot conversion is allowed in the shared core.
    Venue adapters must establish the units before constructing this contract.
    """
    def __init__(self, symbol, market_id, base_asset, quote_asset, contract_type,
                 quantity_unit, multiplier, units_verified, size_step, price_step,
                 min_size, min_quote, order_quote_limit='1E30', raw=None):
        if (contract_type != 'linear_perpetual' or quantity_unit != 'base'
                or number(multiplier) != 1 or units_verified is not True
                or quote_asset != 'USD' or not base_asset):
            raise Reject('unsupported or unverified contract units/multiplier/quote')
        self.symbol, self.market_id = symbol, market_id
        self.base_asset, self.quote_asset = base_asset, quote_asset
        self.contract_type, self.quantity_unit = contract_type, quantity_unit
        self.multiplier, self.units_verified = number(multiplier), units_verified
        self.size_step, self.price_step = number(size_step, True), number(price_step, True)
        self.min_size, self.min_quote = number(min_size, True), number(min_quote)
        if self.min_quote < 0:
            raise Reject('negative minimum notional')
        self.order_quote_limit = number(order_quote_limit, True)
        self.raw = raw or {}

    def validate(self, qty, price):
        qty, price = number(qty, True), number(price, True)
        if qty % self.size_step:
            raise Reject('size precision violation; no silent rounding')
        if price % self.price_step:
            raise Reject('price precision violation')
        if qty < self.min_size:
            raise Reject('below min size')
        if qty * price < self.min_quote:
            raise Reject('below min notional')
        if qty * price > self.order_quote_limit:
            raise Reject('order quote limit exceeded')


def Market(raw):
    """Compatibility constructor; Lighter wire parsing is adapter-owned."""
    from .lighter import normalize_market
    return normalize_market(raw)


class Book:
    def __init__(self, raw, observed_at):
        self.observed_at = observed_at
        self.sides = {}
        for name in ('asks', 'bids'):
            orders = [(number(o['price'], True), number(o['remaining_base_amount']))
                      for o in raw[name]]
            if any(q < 0 for _, q in orders):
                raise Reject('negative remaining amount')
            self.sides[name] = sorted([(p, q) for p, q in orders if q > 0],
                                      reverse=name == 'bids')
        if not all(self.sides.values()):
            raise Reject('empty book')
        if self.sides['bids'][0][0] >= self.sides['asks'][0][0]:
            raise Reject('crossed book')

    def walk(self, side, quantity, now, max_age):
        quantity = number(quantity, True)
        if side not in ('buy', 'sell'):
            raise Reject('invalid side')
        if not 0 <= now - self.observed_at <= max_age:
            raise Reject('stale book or clock moved backwards')
        left, notional, fills = quantity, Decimal(0), []
        orders = self.sides['asks' if side == 'buy' else 'bids']
        for price, available in orders:
            take = min(left, available)
            notional += take * price
            fills.append({'price': price, 'quantity': take})
            left -= take
            if left == 0:
                break
        if left:
            raise Reject('insufficient visible depth (100-order snapshot is not full liquidity)')
        vwap = notional / quantity
        best = orders[0][0]
        return {'vwap': vwap, 'notional': notional, 'worst_price': fills[-1]['price'],
                'quantity': quantity, 'fills': fills,
                'slippage_bps': abs(vwap / best - 1) * 10000}


def preview(*args, **kwargs):
    """Version-1 public API compatibility, not the universal intent interface."""
    from .compat import preview as legacy_preview
    return legacy_preview(*args, **kwargs)


from dataclasses import dataclass


@dataclass(frozen=True)
class Leg:
    connection_id: str
    instrument: str
    side: str
    fee_bps: str


@dataclass(frozen=True)
class HedgeIntent:
    leg_a: Leg  # primary
    leg_b: Leg  # hedge each actual primary partial
    quantity_base: str

    def validate(self, registry):
        number(self.quantity_base, True)
        if self.leg_a.side not in ('buy', 'sell') or self.leg_b.side not in ('buy', 'sell'):
            raise Reject('invalid leg side')
        if self.leg_a.side == self.leg_b.side:
            raise Reject('hedge legs must have opposite sides')
        connections = [registry.connection(l.connection_id) for l in (self.leg_a, self.leg_b)]
        if connections[0].scope == connections[1].scope:
            raise Reject('hedge requires distinct account/deployment connections')
        for leg in (self.leg_a, self.leg_b):
            if not leg.instrument or number(leg.fee_bps) < 0:
                raise Reject('invalid instrument or fee')
            registry.capabilities(leg.connection_id).require('public_market_data')


def preview_intent(intent, registry, max_notional, max_cost, now, max_age):
    """Adapter-neutral risk preview of an explicit pair, in verified base units.

    Sequential bounded snapshots are not atomic venue observations. No private
    capabilities or signing authority are used or implied by this public preview.
    """
    intent.validate(registry)  # all identities and sides checked before network
    qty, cap, cost_cap = number(intent.quantity_base, True), number(max_notional, True), number(max_cost, True)
    legs, exits, fees, deployments, constraints = {}, {}, {}, {}, {}
    fee_entry = fee_exit = signed_entry = signed_exit = Decimal(0)
    base = None
    specifications = (('leg_a', intent.leg_a), ('leg_b', intent.leg_b))
    snapshots = {key: registry.public(spec.connection_id).snapshot(spec.instrument)
                 for key, spec in specifications}
    # Production callers provide a clock, evaluated AFTER both public reads.
    checked_at = now() if callable(now) else now
    # Entropy requires reviewed issuer/base/price units, not a common ticker.
    if any(registry.connection(spec.connection_id).deployment.venue == 'entropy'
           for _, spec in specifications):
        from .entropy import validate_pair
        validate_pair([(registry.connection(spec.connection_id), snapshots[key][0])
                       for key, spec in specifications])
    if any(registry.connection(spec.connection_id).deployment.venue == 'evedex'
           for _, spec in specifications):
        from .evedex import validate_pair as validate_evedex_pair
        validate_evedex_pair([(registry.connection(spec.connection_id), snapshots[key][0])
                              for key, spec in specifications])
    if any(registry.connection(spec.connection_id).deployment.venue == 'hyperliquid'
           for _, spec in specifications):
        from .hyperliquid import validate_pair as validate_native_pair
        validate_native_pair([(registry.connection(spec.connection_id), snapshots[key][0])
                              for key, spec in specifications])
    for key, spec in specifications:
        conn = registry.connection(spec.connection_id)
        market, book, provenance = snapshots[key]
        if not isinstance(market, Instrument):
            raise Reject('adapter must provide verified normalized Instrument')
        if base is not None and base != market.base_asset:
            raise Reject('base asset mismatch between legs')
        base = market.base_asset
        close_side = 'sell' if spec.side == 'buy' else 'buy'
        entry = book.walk(spec.side, qty, checked_at, max_age)
        close = book.walk(close_side, qty, checked_at, max_age)
        for estimate in (entry, close):
            market.validate(qty, estimate['worst_price'])
            if any(fill['price'] % market.price_step for fill in estimate['fills']):
                raise Reject('book price precision violation')
            if max(estimate['notional'], qty * estimate['worst_price']) > cap:
                raise Reject('max notional per leg exceeded')
        legs[key] = dict(entry, side=spec.side, market_id=market.market_id,
                         instrument=spec.instrument, connection_id=conn.id,
                         venue=conn.deployment.venue, deployment_id=conn.deployment.id,
                         account_identity=conn.account_identity, inventory_key=conn.inventory_key,
                         account=conn.inventory_key, base_asset=market.base_asset,
                         quote_asset=market.quote_asset, quantity_unit=market.quantity_unit,
                         contract_type=market.contract_type, multiplier=market.multiplier,
                         units_verified=True, size_step=market.size_step,
                         min_size=market.min_size, min_notional=market.min_quote,
                         inventory_source='local synthetic only; actual account NOT read')
        exits[key], fees[key] = close, number(spec.fee_bps)
        deployments[key], constraints[key] = provenance, market.raw
        fee_entry += entry['notional'] * fees[key] / 10000
        fee_exit += close['notional'] * fees[key] / 10000
        signed_entry += entry['notional'] * (1 if spec.side == 'buy' else -1)
        signed_exit += close['notional'] * (1 if close_side == 'buy' else -1)
    total = signed_entry + signed_exit + fee_entry + fee_exit
    if total > cost_cap:
        raise Reject('roundtrip cost cap exceeded')
    return {'schema_version': 2, 'mode': 'PUBLIC_PREVIEW_TRADING_DISABLED',
            'symbol': base, 'quantity_base': qty, 'direction': 'explicit_two_leg',
            'roles': {'primary': 'leg_a', 'hedge': 'leg_b'},
            'legs': legs, 'exit_estimate': exits, 'deployments': deployments,
            'market_constraints': constraints,
            'entry_cash_cost_usd': signed_entry, 'entry_fee_usd': fee_entry,
            'exit_fee_usd': fee_exit, 'roundtrip_cost_usd': total,
            'fees_bps_assumed_not_account_verified': fees,
            'max_notional_per_leg_usd': cap, 'max_roundtrip_cost_usd': cost_cap,
            'funding': 'NOT_FETCHED_OR_INCLUDED',
            'warning': 'Static public snapshots; synthetic execution only. No guaranteed exit, flatten or profit.'}
