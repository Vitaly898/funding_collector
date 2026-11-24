"""Loopback-only single-owner live UI. Separate from the legacy paper HTTP app.

make_server(root, *, host='127.0.0.1', port=8765, origin=None,
            service_factory=None, session_ttl=900) -> HTTPServer
The factory seam supplies a service only, never bypasses owner authentication.
"""
import argparse
import getpass
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import resource
import threading
from urllib.parse import urlsplit
import warnings
from .domain import Reject
from .web_auth import OwnerAuth, Sessions, RateLimit, initialize_owner

ASSETS=Path(__file__).with_name('live_ui')


def unique(pairs):
    out={}
    for k,v in pairs:
        if k in out:raise ValueError('duplicate')
        out[k]=v
    return out


class Server(ThreadingHTTPServer):
    daemon_threads=True
    block_on_close=False
    def handle_error(self,request,address):pass
    def server_close(self):
        if hasattr(self,'sessions'):self.sessions.close()
        if hasattr(self,'service'):self.service.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version='Live'
    sys_version=''
    protocol_version='HTTP/1.0'
    def setup(self):
        super().setup();self.connection.settimeout(5)
    def log_message(self,*args):pass
    def log_error(self,*args):pass
    def send_error(self,code,message=None,explain=None):
        self.reply(code,{'error':'invalid_request'})
    def reply(self,code,data,*,cookie=None,content_type='application/json'):
        raw=json.dumps(data,separators=(',',':'),allow_nan=False).encode() if content_type=='application/json' else data
        self.send_response(code)
        for k,v in {'Content-Type':content_type,'Content-Length':str(len(raw)),'Cache-Control':'no-store',
                    'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
                    'X-Frame-Options':'DENY','Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"}.items():self.send_header(k,v)
        if cookie:self.send_header('Set-Cookie',cookie)
        self.end_headers();self.wfile.write(raw);self.close_connection=True
    def cookie(self,name,value,*,clear=False):
        return f'{name}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={0 if clear else 900}'+('; Secure' if self.server.secure else '')
    def cookies(self):
        values=self.headers.get_all('Cookie',[])
        if len(values)>1:raise ValueError()
        c=SimpleCookie();c.load(values[0] if values else '')
        return c
    def token(self,name):
        c=self.cookies();return c[name].value if name in c else ''
    def boundary(self):
        s=self.server
        if self.headers.get_all('Host',[])!=[s.public_host]:return 403
        origin=self.headers.get_all('Origin',[])
        if (self.command=='POST' and origin!=[s.origin]) or (origin and origin!=[s.origin]):return 403
        if self.headers.get_all('Transfer-Encoding',[]):return 400
        lengths=self.headers.get_all('Content-Length',[])
        if len(lengths)>1:return 400
        if lengths:
            if not lengths[0].isascii() or not lengths[0].isdigit() or len(lengths[0])>8:return 400
            if int(lengths[0])>16384:return 413
        if self.command=='POST' and not lengths:return 400
        if len(self.headers.get_all('X-CSRF-Token',[]))>1:return 403
        return None
    def body(self,fields):
        if self.headers.get_all('Content-Type',[])!=['application/json']:raise ValueError()
        n=int(self.headers['Content-Length']);raw=self.rfile.read(n)
        if len(raw)!=n:raise ValueError()
        data=json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        if not isinstance(data,dict) or set(data)!=fields:raise ValueError()
        return data
    def do_GET(self):self.dispatch()
    def do_POST(self):self.dispatch()
    def dispatch(self):
        try:self.route()
        except (BrokenPipeError,ConnectionResetError,TimeoutError):pass
        except (ValueError,TypeError,KeyError,UnicodeError):self.reply(400,{'error':'invalid_request'})
        except Reject:self.reply(409,{'error':'operation_refused'})
        except Exception:self.reply(500,{'error':'operation_failed'})
    def route(self):
        s=self.server
        code=self.boundary()
        if code:return self.reply(code,{'error':'request_rejected'})
        path=self.path
        if self.command=='GET' and path in ('/','/app.js','/style.css'):
            name={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}[path]
            return self.reply(200,(ASSETS/name).read_bytes(),content_type={'/':'text/html; charset=utf-8','/app.js':'text/javascript; charset=utf-8','/style.css':'text/css; charset=utf-8'}[path])
        if self.command=='GET' and path=='/api/health':return self.reply(200,{'ok':True})
        csrf=s.sessions.validate(self.token('fc_session'))
        if self.command=='GET' and path=='/api/auth/status':
            if csrf:return self.reply(200,dict(authenticated=True,csrf_token=csrf,configured=True))
            challenge=s.sessions.challenge(self.token('fc_challenge'))
            return self.reply(200,dict(authenticated=False,csrf_token=challenge,configured=True),cookie=self.cookie('fc_challenge',challenge))
        header=self.headers.get('X-CSRF-Token','')
        if self.command=='POST' and path=='/api/auth/login':
            challenge=self.token('fc_challenge')
            if not s.sessions.challenge_valid(challenge) or not hmac.compare_digest(challenge,header):return self.reply(403,{'error':'request_rejected'})
            if not s.login_limit.allow() or not s.kdf.acquire(blocking=False):return self.reply(429,{'error':'rate_limited'})
            try:
                data=self.body({'password'})
                if not s.owner.verify(data['password']):
                    result=(401,{'error':'login_failed'},None)
                else:
                    token,csrf=s.sessions.issue()
                    result=(200,dict(authenticated=True,csrf_token=csrf,configured=True),self.cookie('fc_session',token))
            finally:s.kdf.release()
            return self.reply(result[0],result[1],cookie=result[2])
        # Authenticate before parsing a private body, including unknown routes.
        if not csrf:return self.reply(401,{'error':'authentication_required'})
        if self.command=='POST' and not hmac.compare_digest(csrf,header):return self.reply(403,{'error':'request_rejected'})
        if self.command=='POST' and path=='/api/auth/logout':
            self.body(set())
            if not s.sessions.revoke_if(self.token('fc_session')):return self.reply(401,{'error':'authentication_required'})
            # Revoked tokens are unusable. Do not emit a delayed cookie deletion
            # that could overwrite a newer login's HttpOnly session cookie.
            return self.reply(200,dict(authenticated=False,csrf_token='',configured=True))
        if self.command=='GET' and path=='/api/live/status':return self.reply(200,s.service.status())
        operations={
            '/api/live/configure':('configure',{'config'}),
            '/api/live/setup':('setup',{'leg','secret','passphrase'}),
            '/api/live/check':('check',{'passphrases'}),
            '/api/live/start':('start',{'passphrases','confirmation'}),
            '/api/live/lock':('stop',set())}
        if self.command!='POST' or path not in operations:return self.reply(404,{'error':'not_found'})
        name,fields=operations[path]
        if path=='/api/live/lock':
            self.body(fields)
            with s.sessions.lock:
                if s.sessions.validate(self.token('fc_session'))!=csrf:return self.reply(401,{'error':'authentication_required'})
                return self.reply(200,s.service.stop())
        if not s.mutation_limit.allow() or not s.kdf.acquire(blocking=False):return self.reply(429,{'error':'rate_limited'})
        try:
            data=self.body(fields)
            token=self.token('fc_session')
            if s.sessions.validate(token)!=csrf:
                status,result=401,{'error':'authentication_required'}
            else:
                s.service._authorization=lambda:s.sessions.validate(token)==csrf
                result=getattr(s.service,name)(**data)
                if s.sessions.validate(token)!=csrf:
                    s.service.stop();status,result=401,{'error':'authentication_required'}
                else:status=200
        finally:s.kdf.release()
        return self.reply(status,result)


def make_server(root,*,host='127.0.0.1',port=8765,origin=None,service_factory=None,session_ttl=900):
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    if host!='127.0.0.1':raise ValueError('loopback bind required')
    owner=OwnerAuth(root)
    if origin is not None:
        p=urlsplit(origin)
        if (p.scheme not in ('https','http') or not p.hostname or p.username or p.password or p.path or p.query or p.fragment
                or origin!=f'{p.scheme}://{p.netloc}' or p.netloc!=p.netloc.lower()):raise ValueError('exact origin required')
        if p.scheme=='http' and (p.hostname!='127.0.0.1' or p.port!=port):raise ValueError('HTTPS required')
    server=Server((host,port),Handler)
    try:
        server.origin=origin or f'http://127.0.0.1:{server.server_port}'
        server.public_host=urlsplit(server.origin).netloc
        server.secure=server.origin.startswith('https://')
        from .live_web_service import LiveService
        server.owner=owner;server.service=(service_factory or LiveService)(root)
        server.sessions=Sessions(server.service.stop,ttl=session_ttl)
        server.login_limit=RateLimit(5,60);server.mutation_limit=RateLimit(12,60)
        server.kdf=threading.BoundedSemaphore(1)
        return server
    except BaseException:server.server_close();raise


def main(argv=None):
    parser=argparse.ArgumentParser(description='Single-owner loopback live prototype; restart locked')
    commands=parser.add_subparsers(dest='command',required=True)
    init=commands.add_parser('init');init.add_argument('--root',required=True,type=Path)
    serve=commands.add_parser('serve');serve.add_argument('--root',required=True,type=Path)
    serve.add_argument('--host',default='127.0.0.1');serve.add_argument('--port',type=int,default=8765);serve.add_argument('--origin')
    args=parser.parse_args(argv)
    try:
        resource.setrlimit(resource.RLIMIT_CORE,(0,0))
        if args.command=='init':
            with warnings.catch_warnings():
                warnings.simplefilter('error',getpass.GetPassWarning)
                password=getpass.getpass('Owner password (minimum 12 characters): ')
                if password!=getpass.getpass('Repeat owner password: '):raise ValueError()
            initialize_owner(args.root,password);del password
            print('Owner initialized; execution locked.');return 0
        server=make_server(args.root,host=args.host,port=args.port,origin=args.origin)
        import signal
        def shutdown(*_):raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM,shutdown)
        try:server.serve_forever()
        finally:server.server_close()
        return 0
    except KeyboardInterrupt:return 0
    except Exception:
        print('Live web operation refused. No automatic cancel or flatten.',file=__import__('sys').stderr);return 1

if __name__=='__main__':raise SystemExit(main())
