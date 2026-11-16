"""One Lighter public implementation, two independent fixed deployments.

The Lighter public order-book schema specifies base amounts and USD prices for
perpetuals. This adapter accepts only that unit-linear subset, never invents a
contract/lot conversion, and rejects contradictory metadata explicitly.
"""
from decimal import Decimal
import time

from .domain import Instrument, Book, Reject, number


def normalize_market(raw):
    if raw.get('market_type') != 'perp' or raw.get('status') != 'active':
        raise Reject('only active perpetual markets supported')
    if raw.get('is_frozen') or raw.get('market_config', {}).get('force_reduce_only'):
        raise Reject('market frozen or reduce-only')
    if number(raw.get('quote_multiplier', '1')) != 1:
        raise Reject('non-unit quote multiplier unsupported')
    size_decimals, price_decimals = int(raw['supported_size_decimals']), int(raw['supported_price_decimals'])
    if not 0 <= size_decimals <= 18 or not 0 <= price_decimals <= 18:
        raise Reject('unsupported decimal precision')
    return Instrument(raw['symbol'], int(raw['market_id']), raw.get('base_asset', raw['symbol']),
                      raw.get('quote_asset', 'USD'), raw.get('contract_type', 'linear_perpetual'),
                      raw.get('quantity_unit', 'base'), raw.get('multiplier', '1'), True,
                      Decimal(10) ** -size_decimals, Decimal(10) ** -price_decimals,
                      raw['min_base_amount'], raw['min_quote_amount'],
                      raw.get('order_quote_limit', '1E30'), raw)


class LighterPublicAdapter:
    def __init__(self, deployment, artifacts):
        from .public import PublicClient
        self.client = PublicClient(deployment, artifacts)

    def snapshot(self, symbol):
        from .public import resolve_market
        raw, _ = self.client.get('orderBookDetails')
        market = resolve_market(raw, symbol)
        raw_book, at = self.client.get('orderBookOrders', market_id=market.market_id, limit=100)
        return market, Book(raw_book, at), {
            'base_url': self.client.deployment.base_url,
            'chain_id': self.client.deployment.chain_id,
            'book_observed_at_unix_s': at,
            'book_age_seconds_at_preview': time.time() - at,
            'exchange_event_timestamp': None,
            'freshness_basis': 'local receipt / conservative HTTP Date and Age, NOT exchange event time'}


# Compatibility alias: one public implementation for both fixed deployments.
LighterCoreRH = LighterPublicAdapter


def default_registry(artifacts):
    from .adapters import Capabilities, Connection, Deployment, Registry
    from .public import DEPLOYMENTS
    registry = Registry()
    for name in DEPLOYMENTS:
        registry.register(Connection(name, Deployment(name, 'lighter'),
                                     'paper-' + name, 'paper-' + name),
                          LighterPublicAdapter(name, artifacts),
                          Capabilities(public_market_data=True))
    from .extended import ExtendedPublicAdapter
    registry.register(Connection('extended', Deployment('starknet-mainnet', 'extended'),
                                 'paper-extended', 'paper-extended'),
                      ExtendedPublicAdapter(artifacts), Capabilities(public_market_data=True))
    from .entropy import EntropyPublicAdapter
    registry.register(Connection('entropy', Deployment('hyperliquid-mainnet-io', 'entropy'),
                                 'paper-entropy', 'paper-entropy'),
                      EntropyPublicAdapter(artifacts), Capabilities(public_market_data=True))
    from .evedex import EvedexPublicAdapter
    registry.register(Connection('evedex', Deployment('production-161803', 'evedex'),
                                 'paper-evedex', 'paper-evedex'),
                      EvedexPublicAdapter(artifacts), Capabilities(public_market_data=True))
    from .hyperliquid import HyperliquidPublicAdapter
    registry.register(Connection('hyperliquid', Deployment('hyperliquid-mainnet-native', 'hyperliquid'),
                                 'paper-hyperliquid', 'paper-hyperliquid'),
                      HyperliquidPublicAdapter(artifacts), Capabilities(public_market_data=True))
    return registry
