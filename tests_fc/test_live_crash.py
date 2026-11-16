"""Actual process death recovery, disk fake exchange only."""
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest


class ProcessRecoveryTests(unittest.TestCase):
    def test_sigkill_at_write_ahead_and_fill_checkpoints(self):
        cwd=Path(__file__).parents[1]
        for stage in ('PRIMARY_PENDING','PRIMARY_FILLS_SAVED','HEDGE_PENDING','HEDGE_FILLS_SAVED'):
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as root:
                args=[sys.executable,'-m','tests_fc.live_worker_probe',root]
                killed=subprocess.run(args+['kill',stage],cwd=cwd,capture_output=True,text=True,timeout=20)
                self.assertEqual(killed.returncode,-signal.SIGKILL,killed.stderr)
                recovered=subprocess.run(args+['resume',stage],cwd=cwd,capture_output=True,text=True,timeout=20)
                self.assertEqual(recovered.returncode,0,recovered.stderr)
                row=json.loads(recovered.stdout)
                if stage in ('PRIMARY_PENDING','HEDGE_PENDING'):
                    self.assertEqual(row['state']['status'],'UNKNOWN');self.assertEqual(row['hedge_calls'],0)
                else:
                    self.assertEqual(row['state']['status'],'COMPLETED_CONFIRMED')
                    self.assertEqual((row['primary_calls'],row['hedge_calls']),(1,1))
                    state=row['state']
                    rerun=subprocess.run(args+['resume',stage],cwd=cwd,capture_output=True,text=True,timeout=20)
                    again=json.loads(rerun.stdout)
                    self.assertEqual(again['state'],state)
                    self.assertEqual(again['hedge_calls'],1)
