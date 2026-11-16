"""Inert capability report; no SDK imports, account reads or filesystem vault discovery."""
from importlib.metadata import PackageNotFoundError,version


def foundation_status():
    pins={'hyperliquid-python-sdk':'0.24.0','lighter-sdk':'1.1.8','cryptography':'50.0.2'}
    deps={}
    for name,pin in pins.items():
        try:installed=version(name)
        except PackageNotFoundError:installed=None
        deps[name]=dict(required=pin,installed=installed,compatible=installed==pin)
    return dict(mode='PRIVATE_FOUNDATION_ONLY',wallet_ready=False,trading_enabled=False,
                credentials_loaded=False,actual_accounts_read=False,background_worker='NOT_STARTED',
                sdk_bridges=['hyperliquid-mainnet-native','lighter-core-mainnet'],dependencies=deps,
                keystore='encrypted custody + isolated bounded signer; local terminal setup/unlock only',
                worker='local supervised CLI: full LIMIT -> one protected IOC; reviewed live acceptance pending',
                browser_execution='UNAVAILABLE; no arm/sign/key/account endpoints',
                missing=['independent verification of signer process/native-library secret isolation and transport behavior',
                         'authoritative transaction/nonce reconciliation and manual unknown-resolution workflow',
                         'independent security review and separately authorized private/live acceptance'])
