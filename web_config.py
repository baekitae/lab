"""Explicit single-owner web deployment configuration; local mode stays loopback."""
import base64,os,secrets
from urllib.parse import urlsplit

class WebConfig:
    def __init__(self,env=None):
        env=os.environ if env is None else env
        self.enabled=env.get('PL_WEB_MODE')=='1'
        self.origin='';self.authority='';self.username='';self.password=''
        if not self.enabled:return
        self.origin=(env.get('PL_PUBLIC_ORIGIN') or env.get('RENDER_EXTERNAL_URL','')).rstrip('/')
        parsed=urlsplit(self.origin)
        if parsed.scheme!='https' or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError('웹 실행에는 HTTPS PL_PUBLIC_ORIGIN 또는 RENDER_EXTERNAL_URL이 필요합니다.')
        self.authority=parsed.netloc
        self.username=env.get('PL_WEB_USER','owner');self.password=env.get('PL_WEB_PASSWORD','')
        if not self.username or ':' in self.username or len(self.password)<24:
            raise ValueError('웹 실행 비밀번호 PL_WEB_PASSWORD는 24자 이상이어야 합니다.')
        if len(env.get('PL_VAULT_MASTER_KEY',''))<32 or not env.get('PL_DATA_DIR'):
            raise ValueError('웹 실행에는 PL_DATA_DIR과 32자 이상의 PL_VAULT_MASTER_KEY가 필요합니다.')
    def authenticated(self,header):
        if not self.enabled:return True
        try:
            scheme,encoded=header.split(' ',1)
            if scheme.lower()!='basic':return False
            user,password=base64.b64decode(encoded,validate=True).decode('utf-8').split(':',1)
            return secrets.compare_digest(user.encode(),self.username.encode()) & secrets.compare_digest(password.encode(),self.password.encode())
        except (ValueError,UnicodeError):return False
