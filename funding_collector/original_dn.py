"""Thin optional bridge to original DN. No SDK/auth/order reimplementation.

Not a live DN engine: original adapters lack MARKET and confirmed-fill APIs.
Import/construction is explicit, never performed by HTTP readiness or paper.
"""
import importlib
import importlib.util

from .domain import Reject, number

ORIGINALS = {
    'extended': ('controller.extended_adapter', 'ExtendedAdapter', ('x10', 'dotenv')),
    'nado': ('controller.nado_adapter', 'NadoAdapter', ('nado_protocol', 'dotenv')),
    'variational': ('controller.variational_adapter', 'VariationalAdapter', ('eth_account', 'dotenv')),
}


def _available(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def connector_status():
    """Dependency discovery only: no env/key reads, SDK imports or connectivity."""
    rows = []
    for venue, (module, cls, deps) in ORIGINALS.items():
        missing = [name for name in deps if not _available(name)]
        if venue == 'extended':
            rows.append(dict(id=venue, implementation='funding_collector.extended.ExtendedPublicAdapter + extended_private.ExtendedSDKBridge',
                missing_dependencies=missing, dependency_check='optional SDK top-level discovery; public uses stdlib',
                live_ready=False, account_checked=False, paper_selectable=True, public_connectivity_checked=False,
                blockers=['Public доступен для preview, сеть проверяется только кнопкой preview',
                          'Private не настроен; live DN не подключён; SDK 2.6 несовместим с original x10.perpetual']))
            continue
        rows.append(dict(id=venue, implementation=module + '.' + cls,
                         missing_dependencies=missing, dependency_check='top-level only, not SDK compatibility',
                         live_ready=False, account_checked=False, paper_selectable=False,
                         blockers=['Нет MARKET/confirmed-fill контракта в original DN',
                                   'Подключение аккаунта и полномочия не проверены']
                                  + (['Не установлены optional dependencies'] if missing else [])))
    for venue in ('core', 'rh'):
        rows.append(dict(id=venue, implementation='funding_collector.lighter.LighterCoreRH',
                         missing_dependencies=[], dependency_check='stdlib public only',
                         live_ready=False, account_checked=False, paper_selectable=True,
                         blockers=['Только public GET + paper; private signer/fill reconciliation отсутствует']))
    rows.append(dict(id='aster', implementation='funding_collector.aster.AsterPublicAdapter',
        missing_dependencies=[], dependency_check='stdlib public; private eth-account optional',
        live_ready=False, account_checked=False, paper_selectable=True,
        blockers=['Private account acceptance unverified; explicit local setup/check/arm required']))
    return rows


def load_original(venue, **kwargs):
    """Construct existing adapter, but NEVER initialize (auth can sign/network)."""
    if venue not in ORIGINALS:
        raise Reject('unknown original DN venue')
    module, name, _ = ORIGINALS[venue]
    try:
        adapter = getattr(importlib.import_module(module), name)(**kwargs)
    except ImportError:
        raise Reject('optional DN SDK unavailable; see connector status') from None
    return OriginalDNBridge(venue, adapter)


class OriginalDNBridge:
    def __init__(self, venue, adapter):
        if venue not in ORIGINALS:
            raise Reject('unknown original DN venue')
        self.venue, self.adapter = venue, adapter

    def _authorize(self, authority, action, instrument, quantity):
        if authority is None:
            raise Reject('explicit scoped authority required; HTTP cannot enable private access')
        authority.authorize(self.venue, action, instrument, quantity)

    async def reconcile(self, instrument, *, authority=None):
        self._authorize(authority, 'account_read', instrument, '0')
        # Exceptions propagate; failure must never become an empty account.
        return dict(position=await self.adapter.get_position(instrument),
                    working_orders=await self.adapter.get_open_orders(instrument),
                    fills_reconciled=False)

    async def limit(self, instrument, side, price, quantity, *, external_id,
                    post_only=True, reduce_only=False, authority=None):
        from controller.models import Side
        if side not in ('buy', 'sell') or not external_id or type(post_only) is not bool or type(reduce_only) is not bool:
            raise Reject('invalid original DN limit parameters')
        price, qty = number(price, True), number(quantity, True)
        # Original contracts are floats. Never silently lose a Decimal digit.
        if number(str(float(price))) != price or number(str(float(qty))) != qty:
            raise Reject('original DN float precision loss')
        self._authorize(authority, 'limit', instrument, str(quantity))
        return await self.adapter.place_limit_order(instrument, Side(side), float(price), float(qty),
                    post_only=post_only, reduce_only=reduce_only, external_id=external_id)

    async def market(self, instrument, side, quantity, *, authority=None):
        raise Reject('original DN has no exact MARKET contract; IOC is not substituted')

    def confirmed_fills(self, order_id):
        raise Reject('original DN has no confirmed fills contract; acknowledgement is not a fill')
