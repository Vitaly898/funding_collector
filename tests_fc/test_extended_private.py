"""OFFLINE private SDK contracts: fakes only, no credentials or network."""
import asyncio
from decimal import Decimal
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock
from funding_collector.domain import Reject
from tests_fc.test_extended import MARKET

class PrivateBridgeTests(unittest.TestCase):
    def test_initialize_reuses_official_client_without_loading_keys(self):
        from funding_collector.extended_private import ExtendedSDKBridge
        from unittest.mock import Mock, patch
        factory = Mock(return_value=NS())
        with patch.dict('sys.modules', {'x10.clients.rest': NS(RestApiClient=factory),
                                       'x10.config': NS(MAINNET_CONFIG='fixed-mainnet')}):
            bridge = ExtendedSDKBridge.initialize(None, instrument_map={'ETH-PERP':'ETH-USD'})
        factory.assert_called_once_with('fixed-mainnet', None)
        self.assertFalse(bridge.trading_enabled)

    def test_default_denies_reads_orders_and_authority_cannot_override(self):
        from funding_collector.extended_private import ExtendedSDKBridge
        client = NS(account=NS(get_positions=AsyncMock()), place_order=AsyncMock())
        bridge = ExtendedSDKBridge(client, instrument_map={'ETH-PERP':'ETH-USD'})
        with self.assertRaisesRegex(Reject, 'disabled'):
            asyncio.run(bridge.reconcile('ETH-PERP'))
        with self.assertRaisesRegex(Reject, 'disabled'):
            asyncio.run(bridge.order('ETH-PERP', 'buy', '.02', '1800', external_id='synthetic', fee_rate='0.0005'))
        client.place_order.assert_not_called()
        client.account.get_positions.assert_not_called()

    def test_local_optin_delegates_true_market_ioc_price_protected_no_fill_claim(self):
        from funding_collector.extended_private import ExtendedSDKBridge
        from unittest.mock import patch
        from enum import StrEnum
        class Side(StrEnum): BUY='BUY'; SELL='SELL'
        class Type(StrEnum): LIMIT='LIMIT'; MARKET='MARKET'
        class TIF(StrEnum): GTT='GTT'; IOC='IOC'
        market = NS(model_dump=lambda **kw: MARKET)
        client = NS(info=NS(get_markets_dict=AsyncMock(return_value={'ETH-USD':market})),
                    place_order=AsyncMock(return_value=NS(data=NS(id=42, external_id='synthetic'))))
        bridge = ExtendedSDKBridge(client, instrument_map={'ETH-PERP':'ETH-USD'}, enable_trading=True)
        with patch.dict('sys.modules', {'x10.models.order': NS(OrderSide=Side, OrderType=Type, TimeInForce=TIF)}):
            result = asyncio.run(bridge.order('ETH-PERP','sell','.02','1800',external_id='synthetic',
                                fee_rate='.0005',order_type='MARKET',post_only=False))
        args = client.place_order.call_args.kwargs
        self.assertEqual((args['order_type'], args['time_in_force'], args['price'], args['amount_of_synthetic']),
                         ('MARKET','IOC',Decimal('1800'),Decimal('.02')))
        self.assertEqual(result, {'order_id':42, 'external_id':'synthetic', 'status':'ACK_ONLY', 'confirmed_fill_base':None})

    def test_private_read_status_uses_exchange_cumulative_fill_not_ack_and_unknown_is_not_zero(self):
        from funding_collector.extended_private import ExtendedSDKBridge
        order = NS(id=42,market='ETH-USD',external_id='synthetic',status='PARTIALLY_FILLED',qty=Decimal('.02'),filled_qty=Decimal('.01'))
        account = NS(get_positions=AsyncMock(return_value=NS(data=[])),
                     get_open_orders=AsyncMock(return_value=NS(data=[])),
                     get_order_by_external_id=AsyncMock(return_value=NS(data=[order])))
        bridge = ExtendedSDKBridge(NS(account=account), instrument_map={'ETH-PERP':'ETH-USD'}, enable_reads=True)
        result = asyncio.run(bridge.order_status('ETH-PERP', external_id='synthetic'))
        self.assertEqual(result['confirmed_fill_base'], Decimal('.01'))
        self.assertFalse(result['complete'])
        self.assertFalse(asyncio.run(bridge.reconcile('ETH-PERP'))['fills_reconciled'])
        account.get_order_by_external_id.return_value = NS(data=[])
        self.assertIsNone(asyncio.run(bridge.order_status('ETH-PERP', external_id='synthetic'))['confirmed_fill_base'])
        account.get_positions.return_value = NS(data=None)
        with self.assertRaises(Reject): asyncio.run(bridge.reconcile('ETH-PERP'))
