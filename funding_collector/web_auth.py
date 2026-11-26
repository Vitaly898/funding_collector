"""Single-owner local provisioning. No passwords or session tokens are persisted.

PBKDF2-SHA256 uses bounded input and constant resource consumption. Owner records
are fixed-schema: an edited iteration count cannot turn verification into a DoS.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import threading
import time

ITERATIONS = 600_000
MAX_SECRET_BYTES = 1024


def bounded_secret(value, *, minimum=1):
    if not isinstance(value, str) or not minimum <= len(value.encode('utf-8')) <= MAX_SECRET_BYTES:
        raise ValueError('invalid secret')
    return value


def private_root(root):
    root = Path(os.path.abspath(root))
    if any(part.is_symlink() for part in (root, *root.parents)):
        raise ValueError('unsafe root')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_dir() or root.stat().st_uid != os.getuid():
        raise ValueError('unsafe root')
    os.chmod(root, 0o700)
    return root


def _derive(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, ITERATIONS).hex()


def initialize_owner(root, password):
    bounded_secret(password, minimum=12)
    root = private_root(root)
    salt = secrets.token_bytes(32)
    record = {'version': 1, 'kdf': 'pbkdf2-sha256', 'iterations': ITERATIONS,
              'salt': salt.hex(), 'hash': _derive(password, salt)}
    fd = os.open(root / 'owner-auth.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(record, output)
        output.flush()
        os.fsync(output.fileno())


class OwnerAuth:
    def __init__(self, root):
        root = private_root(root)
        fd = os.open(root / 'owner-auth.json', os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 2048:
                raise ValueError('invalid auth record')
            record = json.load(source)
        if (not isinstance(record, dict) or set(record) != {'version', 'kdf', 'iterations', 'salt', 'hash'}
                or record['version'] != 1 or record['kdf'] != 'pbkdf2-sha256'
                or record['iterations'] != ITERATIONS):
            raise ValueError('invalid auth record')
        try:
            self.salt = bytes.fromhex(record['salt'])
            self.password_hash = bytes.fromhex(record['hash'])
        except (ValueError, TypeError):
            raise ValueError('invalid auth record') from None
        if len(self.salt) != 32 or len(self.password_hash) != 32:
            raise ValueError('invalid auth record')

    def verify(self, password):
        try:
            bounded_secret(password)
        except (ValueError, UnicodeError):
            return False
        return hmac.compare_digest(bytes.fromhex(_derive(password, self.salt)), self.password_hash)


class RateLimit:
    """Process-wide bucket: spoofed proxy headers cannot obtain fresh capacity."""
    def __init__(self, capacity, window):
        self.capacity, self.window = capacity, window
        self.events = []
        self.lock = threading.Lock()

    def allow(self):
        with self.lock:
            now = time.monotonic()
            self.events = [t for t in self.events if now - t < self.window]
            if len(self.events) >= self.capacity:
                return False
            self.events.append(now)
            return True


class Sessions:
    """One absolute-TTL owner session. Timers revoke authority without HTTP traffic."""
    def __init__(self,on_revoke,*,ttl=900):
        if not 0<ttl<=900:raise ValueError('invalid ttl')
        self.ttl=ttl;self.on_revoke=on_revoke
        self.lock=threading.RLock();self.record=None;self.timer=None;self.generation=0
        self.challenge_key=secrets.token_bytes(32)

    @staticmethod
    def digest(token):return hashlib.sha256(token.encode()).digest()

    def issue(self):
        self.revoke()
        token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
        with self.lock:
            generation=self.generation
            self.record=(self.digest(token),csrf,time.monotonic()+self.ttl)
            self.timer=threading.Timer(self.ttl,self._expire,args=(generation,))
            self.timer.daemon=True;self.timer.start()
        return token,csrf

    def validate(self,token):
        if not isinstance(token,str) or len(token)>128:return None
        with self.lock:
            r=self.record
            if r and time.monotonic()<r[2] and hmac.compare_digest(r[0],self.digest(token)):return r[1]
        return None

    def _expire(self,generation):
        with self.lock:
            if generation!=self.generation:return
            self.revoke()

    def revoke_if(self,token):
        with self.lock:
            if self.validate(token) is None:return False
            self.revoke()
            return True

    def revoke(self):
        with self.lock:
            self.generation+=1
            self.record=None
            if self.timer:self.timer.cancel();self.timer=None
            # Serialize fencing with issuance: a late timer cannot kill a fresh session.
            self.on_revoke()

    def challenge(self,cookie=None):
        if not self.challenge_valid(cookie):
            value=secrets.token_urlsafe(24)
            cookie=value+'.'+hmac.new(self.challenge_key,value.encode(),'sha256').hexdigest()
        return cookie

    def challenge_valid(self,cookie):
        if not isinstance(cookie,str) or len(cookie)>128:return False
        try:value,mac=cookie.split('.')
        except ValueError:return False
        return hmac.compare_digest(mac,hmac.new(self.challenge_key,value.encode(),'sha256').hexdigest())

    def close(self):self.revoke()
