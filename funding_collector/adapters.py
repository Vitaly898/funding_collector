"""Explicit in-process contracts. No dynamic plugins, URLs, credentials or SDKs.

Public data, optional private observation/execution, and signing authority are
separate boundaries. Declaring a capability never grants signing permission.
No production private implementation or signer is supplied by this prototype.
"""
from dataclasses import dataclass, fields
from typing import Protocol

from .domain import Reject


@dataclass(frozen=True)
class Capabilities:
    public_market_data: bool = False
    account_read: bool = False
    execution: bool = False
    ioc: bool = False
    post_only: bool = False
    reduce_only: bool = False
    client_order_id: bool = False
    reconcile_unknown: bool = False

    def require(self, name):
        if name not in {f.name for f in fields(self)} or getattr(self, name) is not True:
            raise Reject('unsupported capability: ' + str(name))


@dataclass(frozen=True)
class Deployment:
    id: str
    venue: str


@dataclass(frozen=True)
class Connection:
    id: str
    deployment: Deployment
    account_identity: str
    inventory_key: str

    @property
    def scope(self):
        # Structural identity, not a caller-chosen alias or an exchange numeric ID.
        return (self.deployment.venue, self.deployment.id, self.account_identity)


class PublicMarketData(Protocol):
    def snapshot(self, instrument: str):
        """Return normalized Instrument, Book, and public provenance dictionary."""
        ...


class AccountReader(Protocol):
    def reconcile(self, connection: Connection):
        """Positions AND working orders, including unknown outcomes; never a default zero."""
        ...


class Execution(Protocol):
    def submit(self, connection: Connection, order, authority): ...
    def cancel(self, connection: Connection, order_id, authority): ...


class SigningAuthority(Protocol):
    def authorize(self, connection: Connection, action: str, instrument: str, quantity):
        """Validate exact deployment/account/action scope; no raw signing from HTTP."""
        ...


class Registry:
    def __init__(self):
        self._entries = {}
        self._private = {}

    def register(self, connection, public, capabilities, *, account_reader=None, execution=None):
        if (capabilities.account_read != (account_reader is not None)
                or capabilities.execution != (execution is not None)
                or (any((capabilities.ioc, capabilities.post_only, capabilities.reduce_only,
                         capabilities.client_order_id, capabilities.reconcile_unknown)) and execution is None)):
            raise Reject('capability declaration lacks matching optional implementation')
        if (connection.id in self._entries or not all((connection.id, connection.deployment.id,
                connection.deployment.venue, connection.account_identity, connection.inventory_key))
                or any(c.inventory_key == connection.inventory_key or c.scope == connection.scope
                       for c, _, _ in self._entries.values())):
            raise Reject('duplicate or invalid connection/account inventory identity')
        capabilities.require('public_market_data')
        self._entries[connection.id] = connection, public, capabilities
        self._private[connection.id] = account_reader, execution

    def account_reader(self, identifier):
        self.capabilities(identifier).require('account_read')
        return self._private[identifier][0]

    def execution(self, identifier):
        self.capabilities(identifier).require('execution')
        return self._private[identifier][1]

    def _entry(self, identifier):
        if identifier not in self._entries:
            raise Reject('unknown connection: ' + str(identifier))
        return self._entries[identifier]

    def connection(self, identifier):
        return self._entry(identifier)[0]

    def public(self, identifier):
        return self._entry(identifier)[1]

    def capabilities(self, identifier):
        return self._entry(identifier)[2]

    def describe(self):
        from dataclasses import asdict
        return [dict(asdict(c), capabilities=asdict(cap)) for c, _, cap in self._entries.values()]
