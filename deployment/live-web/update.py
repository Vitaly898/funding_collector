#!/usr/bin/env python3
"""Fail-closed, operator-only Funding Collector update; never initializes data.
Run from the clean trusted clone. No automatic activation or account access.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

REMOTE='https://github.com/Vitaly898/funding_collector.git'
NAME='funding-live-web'
VOLUME='funding_live_web_data'
CONFIRM='WORKER STOPPED; VENUE ORDERS AND POSITIONS RECONCILED'


def command(args, *, cwd=None):
    env={k:v for k,v in os.environ.items() if k!='GIT_CURL_VERBOSE' and not k.startswith('GIT_TRACE')}
    result=subprocess.run(args,cwd=cwd,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=1800)
    if result.returncode:raise RuntimeError('Command failed: '+args[0]+' '+args[1]+' (output suppressed)')
    return result.stdout.strip()


def update(repo, sha, *, runner=command, confirm=input):
    if not re.fullmatch('[0-9a-f]{40}',sha):raise ValueError('Full lowercase 40-character SHA required')
    repo=Path(repo).resolve()
    def git(*args):return runner(['git','-c','core.hooksPath=/dev/null',*args],cwd=repo)
    if Path(git('rev-parse','--show-toplevel')).resolve()!=repo:raise ValueError('Run at repository root')
    if git('remote','get-url','origin')!=REMOTE:raise ValueError('Untrusted repository origin')
    if git('status','--porcelain'):raise ValueError('Dirty tree; preserve changes and stop')
    # Exact remote tips, not cached branches, mutable master or a supplied arbitrary object.
    git('fetch','--prune','--no-tags','origin','+refs/heads/*:refs/remotes/origin/*')
    tips=git('for-each-ref','--format=%(objectname)','refs/remotes/origin/').splitlines()
    if sha not in tips:raise ValueError('SHA is not a freshly verified trusted remote branch tip')
    parent=repo.parent/'.funding-live-builds';parent.mkdir(mode=0o700,exist_ok=True)
    checkout=Path(tempfile.mkdtemp(prefix='release-',dir=parent));checkout.rmdir()
    image='funding-collector-live-web:'+sha[:12]
    backup=NAME+'-recovery-'+str(time.time_ns())
    switched=False;renamed=False;candidate=False;old=None
    def verify_health():
        health="import json,time,urllib.request; time.sleep(.2); r=urllib.request.urlopen('http://127.0.0.1:18765/api/health',timeout=3); assert json.load(r)=={'ok':True}"
        for attempt in range(10):
            try:
                runner(['docker','exec',NAME,'python','-c',health]);return
            except RuntimeError:
                if attempt==9:raise
    try:
        git('worktree','add','--detach',str(checkout),sha)
        runner(['docker','build','--label','org.opencontainers.image.revision='+sha,'-f','deployment/live-web/Dockerfile','-t',image,'.'],cwd=checkout)
        runner(['docker','run','--rm','--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--entrypoint','python',image,'-m','pip','check'])
        smoke="import sys; import funding_collector.live_web; assert not any(m in sys.modules for m in ('hyperliquid','lighter','starknet_py')); print('nonprivate smoke ok')"
        runner(['docker','run','--rm','--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--entrypoint','python',image,'-c',smoke])
        runner(['docker','volume','inspect','--format','{{.Name}}',VOLUME])
        # Never print inspect environments, credentials, private ledger or full Docker metadata.
        template='{"image":{{json .Image}},"running":{{json .State.Running}},"network":{{json .HostConfig.NetworkMode}},"mounts":{{json .Mounts}}}'
        old=json.loads(runner(['docker','container','inspect','--format',template,NAME]))
        if not old['running'] or old['network']!='host' or not old['image'].startswith('sha256:'):
            raise ValueError('Expected running host-network Funding Collector container')
        mounts=old['mounts']
        if len(mounts)!=1 or any(m.get('Type')!='volume' or m.get('Name')!=VOLUME or m.get('Destination')!='/data' or not m.get('RW') for m in mounts):
            raise ValueError('Unexpected mounts; no rollout')
        active=runner(['docker','ps','--quiet','--filter','volume='+VOLUME]).splitlines()
        if len(active)!=1:raise ValueError('Expected exactly one running owner of the data volume')
        print('Built '+sha+' → '+image+'. Existing data and old container will be retained.')
        if confirm('Stop worker in UI, reconcile orders/positions on BOTH venues. Restart does not cancel/flatten.\nType '+CONFIRM+': ')!=CONFIRM:
            raise ValueError('Operator reconciliation confirmation required')
        runner(['docker','stop','--time','30',NAME]);switched=True
        runner(['docker','rename',NAME,backup]);renamed=True
        candidate=True
        runner(['docker','run','--detach','--name',NAME,'--network=host','--memory=1g','--memory-swap=1g','--cpus=1','--pids-limit=64','--read-only','--tmpfs','/tmp:rw,noexec,nosuid,size=64m','--cap-drop=ALL','--security-opt=no-new-privileges','--ulimit=core=0','--restart=no','--volume',VOLUME+':/data',image,'serve','--root','/data','--host','127.0.0.1','--port','18765'])
        # Health inside the exact new container, with bounded retries and no private route.
        verify_health()
        print('Healthy '+sha+'; '+image+'; loopback18765; memory1g/no swap CPU1 pids64 readonly. Recovery container: '+backup+' image '+old['image']+'. Authority remains locked after restart.')
        return dict(sha=sha,image=image,recovery=backup)
    except BaseException:
        if switched:
            try:
                if candidate:
                    # docker run can fail before creating anything. Do not let an
                    # absent candidate prevent restoring the retained old container.
                    exists=runner(['docker','ps','--all','--quiet','--filter','name=^/'+NAME+'$'])
                    if exists:
                        runner(['docker','rm','--force',NAME])  # NEVER -v.
                if renamed:runner(['docker','rename',backup,NAME])
                runner(['docker','start',NAME])
                restored=json.loads(runner(['docker','container','inspect','--format',template,NAME]))
                if restored['image']!=old['image'] or not restored['running']:
                    raise RuntimeError('Previous container identity/running state not restored')
                verify_health()
                print('Previous container restored and healthy; authority remains locked. Reconcile manually.')
            except Exception:
                print('ROLLBACK FAILED. Do not start a second worker. Recovery container: '+backup+'; previous image '+str(old['image'] if old else 'unknown'))
        raise
    finally:
        if checkout.exists():
            try:git('worktree','remove',str(checkout))
            except Exception:print('Build checkout retained: '+str(checkout))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('sha');p.add_argument('--repo',type=Path,default=Path.home()/'apps/funding_collector');p.add_argument('--docker-sudo',action='store_true',help='Use sudo only for Docker commands; Git/build checkout remain owned by the operator');args=p.parse_args()
    def operator_command(argv, **kwargs):
        if args.docker_sudo and argv[0]=='docker':
            argv=['sudo','-n','--',*argv]
        return command(argv,**kwargs)
    lock=Path.home()/'.funding-live-update.lock'
    with lock.open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        def interrupted(*_):raise InterruptedError('update interrupted')
        signal.signal(signal.SIGTERM,interrupted)
        try:update(args.repo,args.sha,runner=operator_command)
        except (Exception,KeyboardInterrupt) as exc:
            print(type(exc).__name__+': update refused/failed; no data initialization performed.');return 1
    return 0

if __name__=='__main__':raise SystemExit(main())
