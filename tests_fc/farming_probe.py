"""SIGKILL acceptance gap using real Worker and independent durable fake exchange."""
import asyncio
from dataclasses import asdict
from decimal import Decimal
import json
import os
from pathlib import Path
import signal
import sys
from funding_collector.live import Order
from funding_collector.farming import FarmingWorker
from tests_fc.test_farming import Venue,policy
from tests_fc.test_live_worker import pair,intent

class DurableVenue(Venue):
    def __init__(self,source,path):
        super().__init__(source);self.path=path
        if path.exists():
            self.calls=[Order(**o) for o in json.loads(path.read_text())]
            self.orders={o.client_id:o for o in self.calls}
    async def submit(self,order):
        ack=await super().submit(order)
        raw=[{k:str(v) if isinstance(v,Decimal) else v for k,v in asdict(o).items()} for o in self.calls]
        pending=self.path.with_suffix('.pending')
        with pending.open('w') as f:json.dump(raw,f);f.flush();os.fsync(f.fileno())
        os.replace(pending,self.path)
        return ack

def offline_factory(spec,secret):
    from tests_fc.test_live_worker import FakeVenue
    from tests_fc.supervised_probe import public_reader
    from funding_collector.keystore import _binding
    if _binding(spec.identity.deployment,spec.identity.account,spec.identity.api_key_index,secret)!=spec.identity:
        raise ValueError('dummy key mismatch')
    path=Path(spec.journal);path.mkdir(mode=0o700,parents=True,exist_ok=True)
    bridge=DurableVenue(FakeVenue(spec.identity,spec.asset_id),path/'fake-exchange.json')
    bridge.market=public_reader(spec)[0];bridge.authority=spec.authority()
    async def registration():bridge.authority.require_read(bridge.identity)
    bridge.registration=registration
    return bridge,lambda:public_reader(spec)


async def main(root,stage):
    root=Path(root);root.mkdir(mode=0o700,parents=True,exist_ok=True)
    p,h=[DurableVenue(v,root/(label+'.json')) for label,v in zip(('primary','hedge'),pair())]
    def checkpoint(current):
        if current==stage:os.kill(os.getpid(),signal.SIGKILL)
    now=[1000]
    with FarmingWorker(root/'worker',p,h,clock=lambda:now[0],checkpoint=checkpoint) as w:
        w.add_farming(intent(),policy()); s=w.farming_state('bounded-test');now[0]=max(1000,s['last_clock'])
        # Opening/restored state stays inactive until explicit new scoped ARM.
        before=w.farming_bytes('bounded-test');await w.tick_farming('bounded-test');assert before==w.farming_bytes('bounded-test')
        w.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
        for _ in range(12):
            s=await w.tick_farming('bounded-test')
            if s['phase']=='HOLDING':now[0]=s['hold_deadline']
            if s['status'] in ('DONE','ATTENTION'):break
        print(json.dumps(dict(status=s['status'],primary=len(p.calls),hedge=len(h.calls))))
if __name__=='__main__':asyncio.run(main(*sys.argv[1:]))
