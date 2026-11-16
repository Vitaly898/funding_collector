"""Read-only allowlisted public transport. No credentials, SDKs or signing."""
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import json
import math
from pathlib import Path
import time
from types import MappingProxyType
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .domain import Market, Reject


@dataclass(frozen=True)
class Deployment:
    name: str
    base_url: str
    chain_id: int  # identification only; no signing capability exists


DEPLOYMENTS = MappingProxyType({
    'core': Deployment('core', 'https://mainnet.zklighter.elliot.ai/api/v1', 304),
    'rh': Deployment('rh', 'https://api.rh.lighter.xyz/api/v1', 466324),
})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Reject('public redirects forbidden')


def resolve_market(raw, symbol):
    if raw.get('code') != 200:
        raise Reject('metadata API error')
    matches = [r for r in raw['order_book_details']
               if r['symbol'] == symbol and r.get('market_type') == 'perp'
               and r.get('status') == 'active']
    if len(matches) != 1:
        raise Reject('market must resolve to exactly one active perpetual: ' + symbol)
    return Market(matches[0])


class PublicClient:
    trading_enabled = False

    def __init__(self, deployment, artifacts):
        if deployment not in DEPLOYMENTS:
            raise Reject('unapproved deployment')
        self.deployment = DEPLOYMENTS[deployment]
        self.artifacts = Path(artifacts)
        self.artifacts.mkdir(parents=True, exist_ok=True)

    def request(self, action='', **kwargs):
        raise Reject('TRADING_DISABLED: only get() with public allowlisted routes is implemented')

    create_order = request
    cancel_order = request
    withdraw = request
    sign = request

    def success(self, raw):
        return isinstance(raw, dict) and raw.get('code') == 200

    def valid_route(self, route, params):
        if route == 'orderBookDetails':
            valid = not params
        elif route == 'orderBookOrders':
            valid = (set(params) == {'market_id', 'limit'}
                     and type(params['market_id']) is int and params['market_id'] >= 0
                     and params['limit'] == 100)
        else:
            valid = False
        return valid

    def get(self, route, **params):
        if not self.valid_route(route, params):
            raise Reject('route/parameters not in public GET allowlist')
        url = self.deployment.base_url + '/' + route
        if params:
            url += '?' + urllib.parse.urlencode(params)
        started = time.time()
        envelope = {'deployment': self.deployment.name, 'chain_id': self.deployment.chain_id,
                    'method': 'GET', 'url': url, 'requested_at_unix_s': started,
                    'body': None, 'freshness_basis': 'request receipt / HTTP cache headers; exchange book event time unavailable'}
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'funding-collector-read-only/0.1',
                                                           'Cache-Control': 'no-cache'}, method='GET')
            opener = urllib.request.build_opener(NoRedirect())
            with opener.open(request, timeout=15) as response:
                body = response.read(12 * 1024 * 1024 + 1)
                envelope['status'] = response.status
                envelope['headers'] = dict(response.headers)
                if len(body) > 12 * 1024 * 1024:
                    raise Reject('public response exceeds limit')
                envelope['body'] = body.decode('utf-8')
                raw = json.loads(envelope['body'])
                if response.status != 200 or not self.success(raw):
                    raise Reject('public API non-success response')
                # Conservative cache freshness. Zero order transaction_time is NOT a book timestamp.
                observed = started
                date = response.headers.get('Date')
                if date:
                    observed = min(observed, parsedate_to_datetime(date).timestamp())
                age = float(response.headers.get('Age', '0'))
                if not math.isfinite(age) or age < 0:
                    raise Reject('invalid HTTP cache age')
                observed = min(observed, started - age)
                envelope['observed_at_unix_s'] = observed
                return raw, observed
        except urllib.error.HTTPError as error:
            envelope['status'] = error.code
            envelope['headers'] = dict(error.headers)
            envelope['error'] = type(error).__name__ + ': ' + str(error)
            with error:
                body = error.read(12 * 1024 * 1024 + 1)
            envelope['body'] = body[:12 * 1024 * 1024].decode('utf-8', errors='replace')
            envelope['body_truncated'] = len(body) > 12 * 1024 * 1024
            raise Reject('public GET failed; no synthetic fallback: ' + str(error)) from error
        except (OSError, ValueError, Reject) as error:
            envelope['error'] = type(error).__name__ + ': ' + str(error)
            raise Reject('public GET failed; no synthetic fallback: ' + str(error)) from error
        finally:
            envelope['received_at_unix_s'] = time.time()
            path = self.artifacts / (self.deployment.name + '-' + route.replace('/', '-') + '-' + uuid.uuid4().hex + '.json')
            path.write_text(json.dumps(envelope, indent=2), encoding='utf-8')
