"""Offline Hyperliquid SDK order contract; deliberately NOT a private connector.

No owner/agent keys, no SDK construction, account reads, approvals, signing or
transport. Payload preparation does not authorize submission. Native MARKET
semantics are a price-protected LIMIT IOC (may partially fill or reject).
"""
from dataclasses import dataclass
from decimal import Decimal
import re
from typing import Literal, TypedDict
from .domain import Reject, number
from .entropy import EntropyInstrument

SDK_VERSION = '0.24.0'
SDK_SOURCE = 'https://github.com/hyperliquid-dex/hyperliquid-python-sdk'


class SDKOrderKwargs(TypedDict):
    name: str
    is_buy: bool
    sz: float
    limit_px: float
    order_type: dict
    reduce_only: bool
    cloid: str  # Caller converts to official Cloid only at a future private boundary.


@dataclass(frozen=True)
class PreparedEntropyOrder:
    coin: str
    asset_id: int
    side: Literal['buy', 'sell']
    quantity: Decimal
    protection_price: Decimal
    tif: Literal['Alo', 'Gtc', 'Ioc']
    client_order_id: str

    def sdk_kwargs(self) -> SDKOrderKwargs:
        # SDK's order method accepts floats. Validate exact Decimal step first;
        # no fee attribution field, no builder approval call, no silent slippage.
        return dict(name=self.coin, is_buy=self.side == 'buy', sz=float(self.quantity),
            limit_px=float(self.protection_price), order_type={'limit': {'tif': self.tif}},
            reduce_only=False, cloid=self.client_order_id)

    def submit(self, *args, **kwargs):
        raise Reject('TRADING_DISABLED: offline Entropy order contract only; private execution not implemented')


def prepare_order(market, side, quantity, protection_price, kind, post_only, client_order_id):
    if (not isinstance(market, EntropyInstrument) or side not in ('buy','sell')
            or kind not in ('LIMIT','MARKET') or type(post_only) is not bool
            or (kind == 'MARKET' and post_only) or protection_price is None
            or not isinstance(client_order_id, str)
            or not re.fullmatch(r'0x[0-9a-fA-F]{32}', client_order_id)):
        raise Reject('invalid Entropy order intent; explicit protection price and 128-bit cloid required')
    market.validate(quantity, protection_price)
    tif = 'Ioc' if kind == 'MARKET' else 'Alo' if post_only else 'Gtc'
    return PreparedEntropyOrder(market.symbol, market.market_id, side, number(quantity,True),
                                number(protection_price,True), tif, client_order_id)
