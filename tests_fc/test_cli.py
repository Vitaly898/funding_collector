"""CLI acceptance uses synthetic plan injection; real smoke is separate."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_paper import plan


def arguments(root, command='paper'):
    return [command, '--store', root, '--intent-id', 'cli-1', '--market', 'ETH',
            '--quantity', '1', '--direction', 'core-long', '--max-notional-usd', '200',
            '--max-roundtrip-cost-usd', '10', '--core-fee-bps', '0', '--rh-fee-bps', '0',
            '--max-unhedged-base', '0.5', '--max-hedge-delay-ms', '1000',
            '--assume-empty-simulated']


class CLI(unittest.TestCase):
    def test_cli_paper_then_retry_without_network_and_status(self):
        from funding_collector.cli import main
        with tempfile.TemporaryDirectory() as root:
            with patch('funding_collector.cli.build_preview', return_value=plan()):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(main(arguments(root)), 0)
            result = json.loads(out.getvalue())
            self.assertEqual(result['status'], 'COMPLETED_SYNTHETIC')
            with patch('funding_collector.cli.build_preview', side_effect=AssertionError('retry fetched network')):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(arguments(root)), 0)
                    self.assertEqual(main(['status', '--store', root, '--intent-id', 'cli-1']), 0)
            self.assertTrue((Path(root) / 'state.json').exists())

    def test_missing_explicit_empty_assumption_rejected_before_public_fetch(self):
        from funding_collector.cli import main
        with tempfile.TemporaryDirectory() as root:
            with patch('funding_collector.cli.build_preview', side_effect=AssertionError('network')):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(arguments(root)[:-1]), 2)

    def test_public_failure_no_simulated_orders_and_error_artifact(self):
        from funding_collector.cli import main
        from funding_collector.domain import Reject
        with tempfile.TemporaryDirectory() as root:
            with patch('funding_collector.cli.build_preview', side_effect=Reject('public outage')):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(arguments(root)), 2)
            self.assertFalse((Path(root) / 'state.json').exists())
            self.assertEqual(len(list(Path(root).rglob('error.json'))), 1)

    def test_module_help_is_safe_and_no_original_private_imports(self):
        proc = subprocess.run([sys.executable, '-m', 'funding_collector', '--help'],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('TRADING DISABLED', proc.stdout)
        # Discovery imports private-SDK test fixtures before execution. Check CLI
        # imports in a clean interpreter, not the already-polluted runner process.
        probe="import sys; from funding_collector.cli import main; forbidden=('controller','dotenv','lighter','web3','eth_account','ccxt'); assert not any(m.split('.')[0] in forbidden for m in sys.modules)"
        imports=subprocess.run([sys.executable,'-c',probe],capture_output=True,text=True,timeout=15)
        self.assertEqual(imports.returncode,0,imports.stderr)
