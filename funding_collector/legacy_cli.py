"""Explicit v1 CLI compatibility: do not normalize historical payloads."""
from concurrent.futures import ThreadPoolExecutor
import time
from .domain import Book, preview
from .paper import atomic_json
from .public import PublicClient, DEPLOYMENTS, resolve_market

LEGACY_FIELDS = ('market', 'direction', 'core_fee_bps', 'rh_fee_bps')


def configure(parser):
    parser.add_argument('--market', help='v1 compatibility: same exact symbol on Core/RH')
    parser.add_argument('--direction', choices=('core-long', 'rh-long'), help='v1 compatibility only')
    parser.add_argument('--core-fee-bps', help='v1 assumed fee bps')
    parser.add_argument('--rh-fee-bps', help='v1 assumed fee bps')


def build_legacy_preview(args, artifacts):
    clients = {venue: PublicClient(venue, artifacts / 'public') for venue in DEPLOYMENTS}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {v: pool.submit(client.get, 'orderBookDetails') for v, client in clients.items()}
        metadata = {v: task.result()[0] for v, task in futures.items()}
        markets = {v: resolve_market(raw, args.market) for v, raw in metadata.items()}
        futures = {v: pool.submit(clients[v].get, 'orderBookOrders', market_id=m.market_id, limit=100)
                   for v, m in markets.items()}
        snapshots = {v: task.result() for v, task in futures.items()}
    books = {v: Book(raw, at) for v, (raw, at) in snapshots.items()}
    result = preview(markets, books, args.market, args.direction, args.quantity,
                     args.max_notional_usd, args.max_roundtrip_cost_usd,
                     args.core_fee_bps, args.rh_fee_bps, time.time(), float(args.max_book_age_seconds))
    result['deployments'] = {v: {'base_url': d.base_url, 'chain_id': d.chain_id,
                                'book_observed_at_unix_s': snapshots[v][1],
                                'book_age_seconds_at_preview': time.time() - snapshots[v][1],
                                'exchange_event_timestamp': None,
                                'freshness_basis': 'local request start and conservative HTTP Date/Age; not proof of event recency'}
                             for v, d in DEPLOYMENTS.items()}
    result['market_constraints'] = {v: m.raw for v, m in markets.items()}
    result['risk'] = {'max_unhedged_base': args.max_unhedged_base,
                      'max_hedge_delay_ms_virtual': args.max_hedge_delay_ms,
                      'max_book_age_seconds': args.max_book_age_seconds}
    result['created_at_unix_s'] = time.time()
    result['artifacts'] = str(artifacts.resolve())
    atomic_json(artifacts / 'preview.json', result)
    return result
