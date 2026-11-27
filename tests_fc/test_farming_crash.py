import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import pytest

@pytest.mark.parametrize('stage',['PRIMARY_PENDING','HEDGE_PENDING','CLOSE_PRIMARY_PENDING','CLOSE_HEDGE_PENDING'])
def test_sigkill_before_send_missing_exchange_order_is_never_submitted(tmp_path,stage):
    command=[sys.executable,'-m','tests_fc.farming_probe',str(tmp_path/'run')]
    killed=subprocess.run(command+[stage],capture_output=True,text=True,timeout=15)
    assert killed.returncode==-signal.SIGKILL,(killed.stdout,killed.stderr)
    expected={'PRIMARY_PENDING':(0,0),'HEDGE_PENDING':(1,0),'CLOSE_PRIMARY_PENDING':(1,1),'CLOSE_HEDGE_PENDING':(2,1)}[stage]
    for _ in range(2):
        resumed=subprocess.run(command+['resume'],capture_output=True,text=True,timeout=15)
        assert resumed.returncode==0,(resumed.stdout,resumed.stderr)
        assert json.loads(resumed.stdout)==dict(status='ATTENTION',primary=expected[0],hedge=expected[1])


@pytest.mark.parametrize('stage',['PRIMARY_SENT','HEDGE_SENT','CLOSE_PRIMARY_SENT','CLOSE_HEDGE_SENT'])
def test_sigkill_after_exchange_accept_before_ack_never_duplicates(tmp_path,stage):
    command=[sys.executable,'-m','tests_fc.farming_probe',str(tmp_path/'run')]
    killed=subprocess.run(command+[stage],capture_output=True,text=True,timeout=15)
    assert killed.returncode==-signal.SIGKILL,(killed.stdout,killed.stderr)
    resumed=subprocess.run(command+['resume'],capture_output=True,text=True,timeout=15)
    assert resumed.returncode==0,(resumed.stdout,resumed.stderr)
    assert json.loads(resumed.stdout)==dict(status='DONE',primary=2,hedge=2)
    for name in ('primary','hedge'):
        orders=json.loads((tmp_path/'run'/f'{name}.json').read_text())
        assert len({o['client_id'] for o in orders})==2
        assert orders[1]['reduce_only'] is True
