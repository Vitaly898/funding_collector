"""Actual CLI masked initialization using a local PTY; synthetic password only."""
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import time


def test_masked_owner_init_cli_and_restart_keeps_hash(tmp_path):
    root=tmp_path/'private'
    password='synthetic-pty-owner-password'
    master,slave=pty.openpty()
    process=subprocess.Popen([sys.executable,'-m','funding_collector.live_web','init','--root',str(root)],
                             stdin=slave,stdout=slave,stderr=slave)
    os.close(slave);output=bytearray()
    def until(marker):
        end=time.monotonic()+10
        while marker not in output:
            assert time.monotonic()<end,'masked init prompt timed out'
            if select.select([master],[],[],.1)[0]:output.extend(os.read(master,4096))
    try:
        until(b'Owner password');os.write(master,(password+'\n').encode())
        until(b'Repeat owner password');os.write(master,(password+'\n').encode())
        process.wait(timeout=10)
        while select.select([master],[],[],0)[0]:
            try:output.extend(os.read(master,4096))
            except OSError:break
        assert process.returncode==0
        assert password.encode() not in output
    finally:
        if process.poll() is None:process.kill();process.wait()
        os.close(master)
    record=root/'owner-auth.json';before=record.read_bytes()
    assert password.encode() not in before
    assert record.stat().st_mode & 0o777==0o600
    result=subprocess.run([sys.executable,'-m','funding_collector.live_web','init','--root',str(root)],
                          input=password+'\n'+password+'\n',capture_output=True,text=True,timeout=10)
    assert result.returncode==1
    assert record.read_bytes()==before


def test_web_cli_help_and_missing_owner_fail_closed(tmp_path):
    prefix=[sys.executable,'-m','funding_collector.live_web']
    assert subprocess.run(prefix+['--help'],capture_output=True,timeout=10).returncode==0
    result=subprocess.run(prefix+['serve','--root',str(tmp_path/'absent'),'--port','0'],capture_output=True,timeout=10)
    assert result.returncode==1
    assert b'operation refused' in result.stderr
