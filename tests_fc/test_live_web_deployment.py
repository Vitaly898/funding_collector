"""Deployment manifest contracts; these do not claim a VPS/container smoke test."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
DEPLOY=ROOT/'deployment'/'live-web'

class LiveDeploymentTests(unittest.TestCase):
    def test_build_context_does_not_include_credentials_or_legacy_stack(self):
        dockerfile=(DEPLOY/'Dockerfile').read_text()
        ignore=(ROOT/'.dockerignore').read_text()
        self.assertIn('requirements-live-web.lock',dockerfile)
        self.assertIn('pip check',dockerfile)
        self.assertIn('USER 10001:10001',dockerfile)
        self.assertNotIn('COPY . ',dockerfile)
        self.assertNotIn('requirements.txt',dockerfile)
        self.assertIn('**/*.env',ignore)
        self.assertIn('**/key.json',ignore)
        self.assertIn('**/*.sqlite3',ignore)
        lock=(ROOT/'requirements-live-web.lock').read_text()
        self.assertIn('eth-account==0.13.7',lock)
        self.assertIn('eth-utils==5.3.1',lock)
        self.assertIn('fast-stark-crypto==0.5.0',lock)
        self.assertIn('da9a79726d23fb605518348dd6f4fa7d3dbd83c9',lock)

    def test_proxy_preserves_auth_origin_and_does_not_expose_admin_or_log_bodies(self):
        caddy=(DEPLOY/'Caddyfile').read_text()
        self.assertIn('admin off',caddy)
        self.assertIn('{$FC_DOMAIN}',caddy)
        self.assertIn('reverse_proxy 127.0.0.1:8765',caddy)
        self.assertNotIn('header_up Host',caddy)
        self.assertNotIn('log {',caddy)

    def test_compose_renders_hardened_app_and_separate_persistent_tls(self):
        self.assertTrue((DEPLOY/'compose.yaml').is_file())
        standalone=os.environ.get('FC_TEST_COMPOSE')
        compose=[standalone] if standalone else ['docker','compose']
        if not shutil.which(compose[0]):
            self.skipTest('Compose executable unavailable; deployment syntax not exercised')
        version=subprocess.run(compose+['version'],capture_output=True,timeout=10)
        if version.returncode:
            self.skipTest('Docker compose plugin unavailable; deployment syntax not exercised')
        result=subprocess.run(compose+['-f',str(DEPLOY/'compose.yaml'),'config','--format','json'],
            capture_output=True,text=True,timeout=15,env=dict(os.environ,FC_DOMAIN='trade.example.test'))
        self.assertEqual(result.returncode,0,result.stderr)
        services=json.loads(result.stdout)['services'];app=services['app'];proxy=services['caddy']
        self.assertEqual(app['user'],'10001:10001')
        self.assertTrue(app['read_only']);self.assertIn('ALL',app['cap_drop'])
        self.assertEqual(proxy['network_mode'],'service:app')
        self.assertEqual({int(p['target']) for p in app['ports']},{80,443})
        self.assertNotIn(8765,{int(p['target']) for p in app['ports']})
        self.assertIn('https://trade.example.test',app['command'])
        self.assertFalse(any('docker.sock' in str(v) for s in services.values() for v in s.get('volumes',[])))
        self.assertFalse(any('env_file' in s for s in services.values()))
        self.assertIn('funding_collector.live_web',app['entrypoint'])

if __name__=='__main__':unittest.main()
