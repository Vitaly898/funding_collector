"""Offline service probe. No production CLI transport-selection seam exists."""
import time
from funding_collector.domain import Instrument,Book


def public_reader(spec,asset_offset=0):
    m=Instrument('ETH',spec.asset_id+asset_offset,'ETH','USD','linear_perpetual','base',1,True,'.0001','.01','.001','10')
    b=Book(dict(bids=[dict(price='2999',remaining_base_amount='1')],
                asks=[dict(price='3001',remaining_base_amount='1')]),time.time())
    return m,b,{}


def offline_factory(spec,secret):
    import socket
    from pathlib import Path
    from decimal import Decimal
    from funding_collector.keystore import _binding
    from tests_fc.live_worker_probe import DurableFake
    def forbidden(*a,**kw):raise AssertionError('network forbidden')
    socket.socket.connect=forbidden
    if _binding(spec.identity.deployment,spec.identity.account,spec.identity.api_key_index,secret)!=spec.identity:
        raise ValueError('dummy identity mismatch')
    path=Path(spec.journal);path.mkdir(mode=0o700,parents=True,exist_ok=True)
    class ReadableDurableFake(DurableFake):
        async def registration(self):
            # Match the real bridge's read-only identity check, without arming.
            self.authority.require_read(self.identity)
    b=ReadableDurableFake(spec.identity,spec.asset_id,path/'fake-exchange.json')
    b.market=public_reader(spec)[0];b.authority=spec.authority()
    b.fraction=Decimal(1);b.result='filled'
    return b,lambda: public_reader(spec)


def run():
    import os
    from pathlib import Path
    import signal
    import sys
    from types import SimpleNamespace
    from funding_collector import supervised as s
    from funding_collector.signer_session import SignerSession
    from tests_fc.test_supervised import KEYS,PASSWORD
    for secret in (*KEYS,PASSWORD):
        assert secret.encode() not in Path('/proc/self/cmdline').read_bytes()
        assert secret.encode() not in Path('/proc/self/environ').read_bytes()
    root,config,action,crash=sys.argv[1:]
    args=SimpleNamespace(root=Path(root),config=Path(config),live_action=action)
    def checkpoint(stage):
        if stage==crash:os.kill(os.getpid(),signal.SIGKILL)
    def sessions(vault,spec):return SignerSession(vault,spec,factory=offline_factory)
    try:
        return s.interactive(args,s.load_config(config,root),reader=public_reader,
                             session_factory=sessions,checkpoint=checkpoint)
    except Exception:
        print('OFFLINE operation refused',flush=True);return 1


if __name__=='__main__':raise SystemExit(run())
