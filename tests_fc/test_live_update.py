"""Operator rollout control tests with fake git/docker; never contacts a daemon."""
import importlib.util
from pathlib import Path
import pytest
SCRIPT=Path(__file__).parents[1]/'deployment/live-web/update.py'
SHA='a'*40

class Fake:
    def __init__(self,mode='ok'):self.calls=[];self.mode=mode;self.new=False
    def __call__(self,args,*,cwd=None):
        self.calls.append(args)
        if args[0]=='git':
            if args[-2:]==['--show-toplevel']:return str(cwd)
            if 'rev-parse' in args:return str(cwd)
            if 'get-url' in args:return 'https://github.com/Vitaly898/funding_collector.git'
            if 'status' in args:return ' M user-file' if self.mode=='dirty' else ''
            if 'for-each-ref' in args:return ('b'*40 if self.mode=='wrong-target' else SHA)+'\n'
            return ''
        if args[1:3]==['ps','--quiet']:return 'old'
        if args[1:3]==['container','inspect']:
            return '{"image":"sha256:old","running":true,"network":"host","mounts":[{"Type":"volume","Name":"funding_live_web_data","Destination":"/data","RW":true}]}'
        if args[1]=='build' and self.mode=='build-failure':raise RuntimeError('build')
        if args[1]=='run' and '--name' in args:
            if self.mode=='run-failure':raise RuntimeError('run before container creation')
            self.new=True
        if args[1]=='rm':
            if not self.new:raise RuntimeError('no candidate container to remove')
            self.new=False
        if args[1:4]==['ps','--all','--quiet']:return 'new' if self.new else ''
        if args[1]=='exec' and self.mode=='health-failure' and self.new:raise RuntimeError('health')
        return ''

def module():
    spec=importlib.util.spec_from_file_location('funding_update',SCRIPT);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

@pytest.mark.parametrize('mode',['ok','build-failure','health-failure','dirty','wrong-target'])
def test_rollout_fail_closed_and_rollback(tmp_path,mode):
    m=module();fake=Fake(mode)
    if mode=='ok':m.update(tmp_path,SHA,runner=fake,confirm=lambda prompt:m.CONFIRM)
    else:
        with pytest.raises((RuntimeError,ValueError)):m.update(tmp_path,SHA,runner=fake,confirm=lambda prompt:m.CONFIRM)
    calls=fake.calls
    stopped=any(c[:2]==['docker','stop'] for c in calls)
    assert stopped==(mode in ('ok','health-failure'))
    if stopped:
        stop=next(i for i,c in enumerate(calls) if c[:2]==['docker','stop'])
        assert any(c[:2]==['docker','build'] for c in calls[:stop])
        assert any('pip' in c and 'check' in c for c in calls[:stop])
        assert any('--entrypoint' in c and 'smoke' in ' '.join(c) for c in calls[:stop])
    if mode=='health-failure':assert any(c[:2]==['docker','start'] for c in calls)
    if mode=='ok':
        fetch=next(c for c in calls if c[0]=='git' and 'fetch' in c)
        assert '--prune' in fetch, 'deleted remote branches must not remain eligible cached tips'
        run=next(c for c in calls if c[:2]==['docker','run'] and '--name' in c)
        for flag in ['--read-only','--cap-drop=ALL','--network=host','--memory=1g','--memory-swap=1g','--cpus=1','--pids-limit=64','--security-opt=no-new-privileges','--ulimit=core=0']:
            assert flag in run
        assert 'funding_live_web_data:/data' in run
    assert not any('prune' in c or '--volumes' in c or 'init' in c for c in calls)

@pytest.mark.parametrize('mode',['run-failure','health-failure'])
def test_failed_candidate_restores_old_even_when_new_was_never_created(tmp_path,mode):
    m=module();fake=Fake(mode)
    with pytest.raises(RuntimeError):m.update(tmp_path,SHA,runner=fake,confirm=lambda prompt:m.CONFIRM)
    assert ['docker','start',m.NAME] in fake.calls
    start=fake.calls.index(['docker','start',m.NAME])
    assert any(c[:3]==['docker','exec',m.NAME] for c in fake.calls[start+1:]), 'rollback must verify previous container health'
    assert not fake.new


def test_confirmation_and_revision_validation(tmp_path):
    m=module();f=Fake()
    with pytest.raises(ValueError):m.update(tmp_path,'master',runner=f)
    assert not f.calls
    with pytest.raises(ValueError):m.update(tmp_path,SHA,runner=f,confirm=lambda prompt:'no')
    assert not any(c[:2]==['docker','stop'] for c in f.calls)
