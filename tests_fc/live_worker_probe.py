"""Subprocess SIGKILL probe: durable FAKE exchange, not production service.

CLI only for tests; no SDK, credentials or network imports. Disk exchange fixture
survives process death so recovery is tested against independent persisted state.
"""
import asyncio
from decimal import Decimal as D
import json
import os
from pathlib import Path
import signal
import sys
from funding_collector.live import Order
from funding_collector.live_worker import Worker
from tests_fc.test_live_worker import FakeVenue,pair,intent


class DurableFake(FakeVenue):
    def __init__(self, identity, asset, path):
        super().__init__(identity,asset);self.path=path
        if path.exists():
            row=json.loads(path.read_text());self.fraction=D(row['fraction']);self.result=row['result']
            self.calls=[Order(**o) for o in row['calls']]
            self.orders={o.client_id:o for o in self.calls}
    def save(self):
        raw=dict(fraction=str(self.fraction),result=self.result,
                 calls=[{k:v if type(v) is bool else str(v) for k,v in o.__dict__.items()} for o in self.calls])
        with self.path.open('w') as f:json.dump(raw,f);f.flush();os.fsync(f.fileno())
    async def submit(self,order):
        ack=await super().submit(order);self.save();return ack
    def fill(self):self.fraction=D(1);self.result='filled';self.save()


async def main(root,action,crash):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    p,h=pair()
    p=DurableFake(p.identity,1,root/'primary-exchange.json')
    h=DurableFake(h.identity,0,root/'hedge-exchange.json')
    def checkpoint(stage):
        if action=='kill' and stage==crash:os.kill(os.getpid(),signal.SIGKILL)
    with Worker(root/'worker',p,h,checkpoint=checkpoint) as w:
        w.add(intent());w.arm('ARM EXACT SCOPED ORDERS',lifetime=30)
        if action=='kill':
            await w.tick('bounded-test');p.fill();await w.tick('bounded-test');h.fill();await w.tick('bounded-test')
        else:
            await w.tick('bounded-test')
            if h.calls and w.state('bounded-test')['status']!='COMPLETED_CONFIRMED':
                h.fill();await w.tick('bounded-test')
        print(json.dumps(dict(state=w.state('bounded-test'),primary_calls=len(p.calls),hedge_calls=len(h.calls))))


if __name__=='__main__':asyncio.run(main(*sys.argv[1:]))
