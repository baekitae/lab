"""Local settings: Windows user-bound DPAPI encryption; memory-only elsewhere."""
from __future__ import annotations
import ctypes
import base64
import hashlib
from datetime import datetime,timezone
import json
import math
import os
import re
import threading
from pathlib import Path
from providers import valid_model
from data_paths import application_data

ROLES = ('TEAM_A','TEAM_B','TEAM_C','TEAM_D','MANAGER_EVAL','RED','DIRECTOR','AUDIT_AI')
SECRET_ROLES=ROLES+('KIS_APPKEY','KIS_APPSECRET')
PUBLIC_FIELDS = {'PL_GEMINI_MODEL','PL_INPUT_KRW_PER_MILLION','PL_OUTPUT_KRW_PER_MILLION',
                 'PL_MONTHLY_BUDGET_KRW','PL_LOCAL_RPD','PL_LOCAL_RPM','PL_LOCAL_TPM'}
PUBLIC_FIELDS.update(f'PL_{r}_{suffix}' for r in ROLES for suffix in ('MODEL','PROJECT_ID','RPD','RPM','TPM'))


def atomic_write(path, raw):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp')
    with tmp.open('wb') as f:
        try: os.chmod(tmp,0o600)
        except OSError: pass
        f.write(raw);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)


class DPAPI:
    def __init__(self):
        if os.name!='nt': raise RuntimeError('Windows DPAPI는 Windows에서만 사용할 수 있습니다.')
        from ctypes import wintypes
        class BLOB(ctypes.Structure):
            _fields_=[('cbData',wintypes.DWORD),('pbData',ctypes.POINTER(ctypes.c_ubyte))]
        self.BLOB=BLOB
        self.crypt=ctypes.WinDLL('crypt32',use_last_error=True)
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.crypt.CryptProtectData.argtypes=[ctypes.POINTER(BLOB),wintypes.LPCWSTR,ctypes.POINTER(BLOB),ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(BLOB)]
        self.crypt.CryptUnprotectData.argtypes=[ctypes.POINTER(BLOB),ctypes.c_void_p,ctypes.POINTER(BLOB),ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(BLOB)]
        self.crypt.CryptProtectData.restype=self.crypt.CryptUnprotectData.restype=wintypes.BOOL
        self.kernel.LocalFree.argtypes=[ctypes.c_void_p];self.kernel.LocalFree.restype=ctypes.c_void_p

    def transform(self,raw,decrypt=False):
        buf=(ctypes.c_ubyte*len(raw)).from_buffer_copy(raw)
        incoming=self.BLOB(len(raw),buf);outgoing=self.BLOB()
        fn=self.crypt.CryptUnprotectData if decrypt else self.crypt.CryptProtectData
        description=None if decrypt else 'Pattern Lab server credentials'
        if not fn(ctypes.byref(incoming),description,None,None,None,1,ctypes.byref(outgoing)):
            raise RuntimeError('Windows 보안 저장소 처리 실패: 같은 Windows 사용자로 실행하고 저장소 상태를 확인하세요.')
        try:return ctypes.string_at(outgoing.pbData,outgoing.cbData)
        finally:self.kernel.LocalFree(outgoing.pbData)


class CloudVault:
    def __init__(self,master):
        from cryptography.fernet import Fernet
        if len(master)<32:raise ValueError('클라우드 암호화 키는 32자 이상이어야 합니다.')
        self.cipher=Fernet(base64.urlsafe_b64encode(hashlib.sha256(master.encode()).digest()))
    def transform(self,raw,decrypt=False):
        return self.cipher.decrypt(raw) if decrypt else self.cipher.encrypt(raw)


class Settings:
    def __init__(self,root,codec=None):
        self.root=Path(root);self.lock=threading.RLock();self.memory={}
        self.codec=codec if codec is not None else CloudVault(os.environ['PL_VAULT_MASTER_KEY']) if os.environ.get('PL_WEB_MODE')=='1' else DPAPI() if os.name=='nt' else None
        self.path=self.root/'settings.json';self.vault=self.root/('credentials.fernet' if isinstance(self.codec,CloudVault) else 'credentials.dpapi')

    def public(self):
        if not self.path.exists():return {}
        try:
            data=json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(data,dict) or set(data)-PUBLIC_FIELDS:raise ValueError()
            return data
        except (ValueError,OSError):raise ValueError('설정 파일을 읽을 수 없습니다. 백업을 보존하고 복구 화면을 확인하세요.') from None

    def secrets(self):
        if not self.codec:return dict(self.memory)
        if not self.vault.exists():return {}
        try:return json.loads(self.codec.transform(self.vault.read_bytes(),decrypt=True))
        except Exception:raise ValueError('보안 저장소를 열 수 없습니다. 원본 파일을 보존하고 같은 OS 사용자로 실행하세요.') from None

    def effective_env(self):
        with self.lock:
            result=self.public()
            for role,key in self.secrets().items():
                result[f'PL_{role}_API_KEY']=key
            result.update(os.environ)
            for name,value in list(result.items()):
                if name in PUBLIC_FIELDS and name.endswith('MODEL') and value:
                    try:result[name]=valid_model(value)
                    except ValueError:pass
            return result

    def save(self,payload):
        if not isinstance(payload,dict) or set(payload)-{'values','keys','delete_keys'}:raise ValueError('설정 요청 형식이 올바르지 않습니다.')
        values=payload.get('values',{});keys=payload.get('keys',{});deletes=payload.get('delete_keys',[])
        if not isinstance(values,dict) or set(values)-PUBLIC_FIELDS or not isinstance(keys,dict) or set(keys)-set(SECRET_ROLES) or not isinstance(deletes,list) or any(x not in SECRET_ROLES for x in deletes):raise ValueError('허용되지 않은 설정 항목입니다.')
        clean={}
        for name,value in values.items():
            if not isinstance(value,(str,int,float)) or isinstance(value,bool):raise ValueError('설정값은 문자 또는 숫자여야 합니다.')
            text=str(value).strip()
            if len(text)>200 or any(ord(c)<32 for c in text):raise ValueError('설정값 형식이 올바르지 않습니다.')
            if text and name.endswith('MODEL'):text=valid_model(text)
            if text and name.endswith('PROJECT_ID') and not re.fullmatch(r'[a-z][a-z0-9-]{4,62}',text):raise ValueError('프로젝트 ID 형식을 확인하세요.')
            if text and ('KRW' in name or name.endswith(('RPD','RPM','TPM'))):
                try:number=float(text)
                except ValueError:raise ValueError('예산·단가는 숫자로 입력하세요.') from None
                if not math.isfinite(number) or number<0:raise ValueError('예산·단가는 0 이상의 유한 숫자여야 합니다.')
                if name=='PL_MONTHLY_BUDGET_KRW' and not 0<number<=20000:raise ValueError('월 예산은 0 초과 20,000원 이하입니다.')
                if name.endswith(('RPD','RPM','TPM')) and (not number.is_integer() or not 1<=number<=1000000):raise ValueError('로컬 호출 제한은 양의 정수여야 합니다.')
            managed=os.environ.get(name)
            if managed and name.endswith('MODEL'):managed=valid_model(managed)
            if name in os.environ and text!=managed:raise ValueError('환경에서 관리하는 설정은 환경 설정에서 변경하세요: '+name)
            clean[name]=text
        for role,key in keys.items():
            if not isinstance(key,str) or len(key)>1024 or (key and (len(key)<8 or any(ord(c)<33 or ord(c)>126 for c in key))):raise ValueError('API 키 형식이 올바르지 않습니다.')
            if key and os.environ.get(f'PL_{role}_API_KEY'):raise ValueError('환경에서 관리하는 역할 키는 환경 설정에서 변경하세요.')
        with self.lock:
            old_public=self.public();old_secrets=self.secrets();updated=dict(old_secrets)
            for role,key in keys.items():
                if key:updated[role]=key
            for role in deletes:
                if os.environ.get(f'PL_{role}_API_KEY'):raise ValueError('환경에서 관리하는 키는 이 화면에서 삭제할 수 없습니다.')
                updated.pop(role,None)
            all_keys=[os.environ.get(f'PL_{r}_API_KEY') or updated.get(r) for r in ROLES]
            present=[k for k in all_keys if k]
            if len(set(present))!=len(present):raise ValueError('역할별 키가 중복되었습니다. 서로 다른 프로젝트 키인지 확인하세요.')
            new_public={**old_public,**clean};old_blob=self.vault.read_bytes() if self.codec and self.vault.exists() else None
            secret_changed=updated!=old_secrets
            # Persist secrets only as an OS-protected blob. On Linux/macOS use process memory.
            if self.codec and secret_changed:
                encrypted=self.codec.transform(json.dumps(updated).encode())
                if old_blob is not None:
                    archive=self.root/'recovery'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-'+self.vault.name+'.preserved')
                    atomic_write(archive,old_blob)
                atomic_write(self.vault,encrypted)
            elif not self.codec:self.memory=updated
            try:atomic_write(self.path,json.dumps(new_public,ensure_ascii=False,indent=2).encode())
            except Exception:
                if self.codec and secret_changed:
                    if old_blob is not None:atomic_write(self.vault,old_blob)
                    elif self.vault.exists():self.vault.unlink()
                elif not self.codec:self.memory=old_secrets
                raise
        return self.view()

    def recover(self,confirmed=False):
        if confirmed is not True:raise ValueError('암호화 원본을 보존한 후 연결 설정을 초기화하는 작업을 확인하세요.')
        with self.lock:
            folder=self.root/'recovery';folder.mkdir(parents=True,exist_ok=True);saved=[]
            for path in (self.path,self.vault):
                if not path.exists():continue
                raw=path.read_bytes();name=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-'+path.name+'.preserved'
                archive=folder/name;atomic_write(archive,raw)
                if archive.read_bytes()!=raw:raise ValueError('원본 보존 검증 실패입니다. 초기화를 중단합니다.')
                path.unlink();saved.append({'file':name,'sha256':hashlib.sha256(raw).hexdigest()})
            self.memory={}
        return {'reset':True,'preserved':saved,'environment_bindings_changed':False}

    def view(self):
        with self.lock:
            error=''
            try:env=self.effective_env()
            except ValueError as exc:env=dict(os.environ);error=str(exc)
            return {'error':error,'values':{k:env[k] for k in PUBLIC_FIELDS if k in env},
                    'roles':[{'role':r,'configured':bool(env.get(f'PL_{r}_API_KEY')),
                              'managed_by_environment':bool(os.environ.get(f'PL_{r}_API_KEY'))} for r in ROLES],
                    'storage':'cloud-encrypted' if isinstance(self.codec,CloudVault) else 'windows-dpapi' if self.codec else 'session-memory',
                    'market_credentials':{r:bool(env.get(f'PL_{r}_API_KEY')) for r in ('KIS_APPKEY','KIS_APPSECRET')},
                    'persistent_keys':bool(self.codec),
                    'note':'서버의 암호화 저장소에 저장됩니다. 학교와 집에서 같은 웹 주소로 접속하면 같은 설정을 사용합니다.' if isinstance(self.codec,CloudVault) else 'Windows 사용자 전용 저장소에 암호화해 보관합니다. 같은 Windows 계정에서는 재실행·프로그램 업데이트 후에도 등록된 키를 사용합니다.' if self.codec else '이 OS에서는 입력한 키를 메모리에만 보관합니다. 서버 종료 후 다시 입력하거나 환경의 보안 값을 사용하세요.'}


SETTINGS=Settings(application_data(Path(__file__).resolve().parent))
