"""Encrypted local key custody foundation. No export API, HTTP route or env loader.

Scrypt (128 MiB) + AES-256-GCM from cryptography, random salt/nonce, authenticated
identity/version. Encryption at rest is not an OS sandbox. Python/SDK copies are
not reliably erasable; this stage deliberately does NOT hydrate SDK signers from
an unlocked vault. Use an isolated funded account; no owner key is accepted for HL.
"""
import argparse
from dataclasses import asdict
import getpass
import hashlib
import json
import os
from pathlib import Path
import select
import stat
import sys
import threading
import time
from .domain import Reject
from .live import Identity

AAD_VERSION='funding-collector-keystore-v1:scrypt-n131072-r8-p1:aes256gcm'
MAX_SECRET_BYTES=8192
EXTENDED_SECRET_VERSION='extended-credentials-v1'


def _binding(deployment, account, key_index, secret):
    try:
        if deployment=='extended-mainnet-starknet':
            from .extended_private import ExtendedCredentials
            if not isinstance(secret,ExtendedCredentials):raise ValueError()
            # Revalidate even a caller-supplied frozen instance before custody.
            checked=ExtendedCredentials(**asdict(secret))
            return Identity(deployment,account,checked.stark_public_key,key_index)
        key=bytes.fromhex(secret.removeprefix('0x'))
        if deployment in ('hyperliquid-mainnet-native','entropy-mainnet-io') and len(key)==32:
            from eth_account import Account
            signer=Account.from_key(key).address
        elif deployment in ('lighter-core-mainnet','lighter-rh-mainnet') and len(key)==40:
            signer=hashlib.sha256(key).hexdigest()
        else:raise ValueError()
        return Identity(deployment,account,signer,key_index)
    except Exception:
        raise Reject('invalid execution key or identity; owner key not accepted as HL agent') from None


def _encode_secret(identity, secret):
    """Trusted bounded pipe payload; old hex plaintext stays byte-for-byte intact.

    Extended Stark credentials are NOT assumed to have agent-only permissions.
    Use a separately funded account; the local scope does not restrict the key.
    """
    try:
        if _binding(identity.deployment,identity.account,identity.api_key_index,secret)!=identity:
            raise ValueError()
        if identity.deployment=='extended-mainnet-starknet':
            value=json.dumps(dict(version=EXTENDED_SECRET_VERSION,credentials=asdict(secret)),
                             sort_keys=True,separators=(',',':')).encode()
        else:
            value=secret.encode()
        if not 1<=len(value)<=MAX_SECRET_BYTES:raise ValueError()
        return value
    except Exception:
        raise Reject('invalid credential payload or scoped binding') from None


def _decode_secret(identity, value):
    """Decode only the credential kind permitted by the authenticated identity."""
    try:
        if not isinstance(value,(bytes,bytearray)) or not 1<=len(value)<=MAX_SECRET_BYTES:
            raise ValueError()
        if identity.deployment=='extended-mainnet-starknet':
            from .extended_private import ExtendedCredentials
            def unique(pairs):
                result={}
                for key,item in pairs:
                    if key in result:raise ValueError()
                    result[key]=item
                return result
            raw=json.loads(value,object_pairs_hook=unique)
            if (not isinstance(raw,dict) or set(raw)!={'version','credentials'}
                    or raw['version']!=EXTENDED_SECRET_VERSION):raise ValueError()
            secret=ExtendedCredentials(**raw['credentials'])
        else:
            secret=value.decode()
        if _binding(identity.deployment,identity.account,identity.api_key_index,secret)!=identity:
            raise ValueError()
        return secret
    except Exception:
        raise Reject('invalid credential payload or scoped binding') from None


def _aad(identity):
    return json.dumps(dict(version=AAD_VERSION,identity=asdict(identity)),sort_keys=True,separators=(',',':')).encode()


def _derive(passphrase,salt):
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    if not isinstance(passphrase,str) or len(passphrase)<16:
        raise Reject('local passphrase must contain at least 16 characters')
    return Scrypt(salt=salt,length=32,n=131072,r=8,p=1).derive(passphrase.encode())


class KeyStore:
    def __init__(self, root):
        self.root=Path(root)
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_mode&0o077:
            raise Reject('keystore directory must be private 0700, not a symlink')
        self.__secret=None;self.__until=0;self.__timer=None;self.__mutex=threading.RLock()
        self.__on_lock=[]

    def __repr__(self):
        return '<KeyStore unlocked='+str(self.unlocked)+' secrets=REDACTED>'

    def _read(self):
        try:
            fd=os.open(self.root/'key.json',os.O_RDONLY|os.O_NOFOLLOW)
            with os.fdopen(fd,'rb') as f:
                s=os.fstat(f.fileno())
                if not stat.S_ISREG(s.st_mode) or s.st_mode&0o077 or s.st_uid!=os.getuid() or s.st_size>8192:
                    raise ValueError()
                raw=json.loads(f.read(8193))
            if set(raw)!={'version','identity','salt','nonce','ciphertext'} or raw['version']!=AAD_VERSION:
                raise ValueError()
            Identity(**raw['identity'])
            return raw
        except Exception:
            raise Reject('keystore missing, unsafe permissions or corrupt; no plaintext fallback') from None

    def identity(self):
        return Identity(**self._read()['identity'])

    def create(self, identity, secret, passphrase):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        value=_encode_secret(identity,secret)
        salt=os.urandom(16);nonce=os.urandom(12)
        ciphertext=AESGCM(_derive(passphrase,salt)).encrypt(nonce,value,_aad(identity))
        raw=dict(version=AAD_VERSION,identity=asdict(identity),salt=salt.hex(),nonce=nonce.hex(),ciphertext=ciphertext.hex())
        try:
            fd=os.open(self.root/'key.json',os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        except OSError:raise Reject('keystore exists or path unsafe; no overwrite') from None
        with os.fdopen(fd,'w') as f:
            json.dump(raw,f);f.flush();os.fsync(f.fileno())
        fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)

    def unlock(self, identity, passphrase, *, lifetime):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self.lock()
        if type(lifetime) is not int or not 1<=lifetime<=900:raise Reject('unlock lifetime must be 1..900 seconds')
        raw=self._read()
        if raw['identity']!=asdict(identity):raise Reject('keystore account/deployment/key scope mismatch')
        try:
            salt=bytes.fromhex(raw['salt']);nonce=bytes.fromhex(raw['nonce'])
            if len(salt)!=16 or len(nonce)!=12:raise ValueError()
            value=AESGCM(_derive(passphrase,salt)).decrypt(nonce,bytes.fromhex(raw['ciphertext']),_aad(identity))
            _decode_secret(identity,value)
        except Exception:raise Reject('unlock refused: authentication or binding failed') from None
        with self.__mutex:
            self.__secret=bytearray(value);self.__until=time.monotonic()+lifetime
            self.__timer=threading.Timer(lifetime,self.lock);self.__timer.daemon=True;self.__timer.start()

    @property
    def unlocked(self):
        with self.__mutex:
            if time.monotonic()>=self.__until:self.lock()
            return self.__secret is not None

    def handoff(self, identity, connection, on_lock):
        """Internal trusted pipe only. Purge parent buffer; retain revocation timer.

        This is not export/serialization support. Python/crypto immutable copies
        cannot be reliably zeroized. Process death fences SDK/native copies.
        """
        with self.__mutex:
            if (identity!=self.identity() or self.__secret is None or time.monotonic()>=self.__until
                    or not 1<=len(self.__secret)<=MAX_SECRET_BYTES):
                raise Reject('locked or mismatched custody handoff')
            self.__on_lock.append(on_lock)
            try:connection.send_bytes(self.__secret)
            finally:
                self.__secret[:]=b'\0'*len(self.__secret);self.__secret=None

    def lock(self):
        with self.__mutex:
            if self.__timer:self.__timer.cancel();self.__timer=None
            if self.__secret is not None:
                self.__secret[:]=b'\0'*len(self.__secret);self.__secret=None
            self.__until=0
            callbacks,self.__on_lock=self.__on_lock,[]
        for callback in callbacks:callback()


def main():
    p=argparse.ArgumentParser(description='Local encrypted execution-key custody only; never enables trading')
    p.add_argument('action',choices=['setup','unlock','status'])
    p.add_argument('--directory',type=Path,default=Path.home()/'.local/share/funding-collector/keystore')
    p.add_argument('--lifetime',type=int,default=300)
    args=p.parse_args()
    try:
        k=KeyStore(args.directory)
        if args.action=='status':
            k.identity();print('Encrypted keystore present; locked. Execution DISABLED.');return
        if not sys.stdin.isatty() or not sys.stderr.isatty():raise Reject('interactive local terminal required; no stdin/argv/env secrets')
        if args.action=='setup':
            deployment=input('Deployment (hyperliquid-mainnet-native / lighter-core-mainnet): ').strip()
            account=input('Public account address/index: ').strip()
            index=int(input('API key index (4..254): ')) if deployment=='lighter-core-mainnet' else None
            secret=getpass.getpass('Dedicated execution key (never owner key): ')
            password=getpass.getpass('New local encryption passphrase (16+ chars): ')
            if password!=getpass.getpass('Repeat passphrase: '):raise Reject('passphrases do not match')
            k.create(_binding(deployment,account,index,secret),secret,password)
            del secret,password
            print('Encrypted keystore created. Locked. No key registration or network request.');return
        password=getpass.getpass('Local encryption passphrase: ')
        k.unlock(k.identity(),password,lifetime=args.lifetime);del password
        print('Custody-only session unlocked. Signer activation NOT implemented; execution DISABLED. Type status or lock.')
        try:
            while k.unlocked:
                ready,_,_=select.select([sys.stdin],[],[],min(1,args.lifetime))
                if ready:
                    command=sys.stdin.readline()
                    if not command or command.strip()=='lock':break
                    print('Unlocked; execution DISABLED.' if k.unlocked else 'Locked.')
        finally:k.lock()
        print('Locked.')
    except (Reject,ValueError,OSError):
        print('Local custody operation refused. No trading enabled.',file=sys.stderr);raise SystemExit(1) from None


if __name__=='__main__':main()
