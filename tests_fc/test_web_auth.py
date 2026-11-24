"""Synthetic credentials only; no exchange or external services."""
import json
import os
import time

import pytest


def test_owner_is_required_and_hash_is_private(tmp_path):
    from funding_collector.web_auth import OwnerAuth, initialize_owner
    with pytest.raises((ValueError, OSError)):
        OwnerAuth(tmp_path)
    initialize_owner(tmp_path, 'synthetic-owner-password')
    auth = OwnerAuth(tmp_path)
    assert auth.verify('synthetic-owner-password')
    assert not auth.verify('incorrect')
    assert not auth.verify('x' * 1025)
    record = tmp_path / 'owner-auth.json'
    assert 'synthetic-owner-password' not in record.read_text()
    assert os.stat(record).st_mode & 0o777 == 0o600
    assert os.stat(tmp_path).st_mode & 0o777 == 0o700
    with pytest.raises((ValueError, OSError)):
        initialize_owner(tmp_path, 'another-owner-password')


def test_sessions_rotate_hash_and_active_expiry_fences():
    from funding_collector.web_auth import Sessions
    stopped=[]
    sessions=Sessions(lambda:stopped.append(True),ttl=.08)
    token,csrf=sessions.issue()
    assert sessions.validate(token)==csrf
    assert token not in repr(sessions.__dict__)
    old=token
    token,csrf=sessions.issue()
    assert sessions.validate(old) is None
    time.sleep(.15)
    assert stopped and sessions.validate(token) is None
    sessions.close()
