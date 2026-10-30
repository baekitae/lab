"""Explicit source-only update from the user's repository; never copy runtime data."""
import io,os,shutil,tempfile,urllib.request,urllib.error,zipfile
from pathlib import Path
from datetime import datetime,timezone

URL='https://github.com/baekitae/lab/archive/refs/heads/main.zip'
EXTENSIONS={'.py','.html','.md','.txt','.bat','.sh','.yaml'}

def update(root,raw):
    root=Path(root)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries={};total=0
        for info in archive.infolist():
            parts=info.filename.split('/')
            if len(parts)<2 or parts[0]!='lab-main' or '..' in parts or '\\' in info.filename or info.filename.startswith('/'):
                raise ValueError('업데이트 압축 경로가 올바르지 않습니다.')
            if info.is_dir():continue
            rel=Path(*parts[1:])
            allowed=(len(rel.parts)==1 or rel.parts[0] in ('docs','scripts','tests'))
            if not allowed or (rel.suffix not in EXTENSIONS and rel.name not in ('.gitignore','.gitattributes')):continue
            if rel.as_posix() in entries:raise ValueError('중복된 업데이트 파일입니다.')
            total+=info.file_size
            if total>30_000_000:raise ValueError('업데이트 크기 한도 초과입니다.')
            if ((info.external_attr>>16)&0o170000)==0o120000:raise ValueError('업데이트 바로가기는 허용하지 않습니다.')
            entries[rel.as_posix()]=archive.read(info)
        if not all(n in entries for n in ('server.py','settings_store.py','data_paths.py','bootstrap.py')):raise ValueError('필수 프로그램 파일이 없습니다.')
    backup=root/'code_backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup.mkdir(parents=True)
    changed=[]
    try:
        for name,content in entries.items():
            dest=root/name
            if any(p.is_symlink() for p in [dest,*dest.parents] if p==root or root in p.parents):raise ValueError('업데이트 경로에 바로가기가 있습니다.')
            existed=dest.exists()
            if existed:
                saved=backup/name;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(dest,saved)
            dest.parent.mkdir(parents=True,exist_ok=True)
            changed.append((name,existed))
            fd,tmp=tempfile.mkstemp(prefix='.pattern-update-',dir=dest.parent)
            try:
                with os.fdopen(fd,'wb') as f:f.write(content)
                os.replace(tmp,dest)
            finally:
                if os.path.exists(tmp):os.unlink(tmp)
    except Exception:
        for name,existed in reversed(changed):
            if existed:shutil.copy2(backup/name,root/name)
            else:(root/name).unlink(missing_ok=True)
        raise
    return len(entries)

def main():
    root=Path(__file__).resolve().parent
    try:
        with urllib.request.urlopen('http://127.0.0.1:8770/api/app/state',timeout=2):
            raise SystemExit('패턴랩 서버 창을 먼저 닫고 업데이트를 다시 실행하세요.')
    except (urllib.error.URLError,TimeoutError):pass
    print('GitHub에서 최신 프로그램을 받습니다. 키와 원장은 변경하지 않습니다.',flush=True)
    with urllib.request.urlopen(URL,timeout=60) as response:raw=response.read(30_000_001)
    if len(raw)>30_000_000:raise ValueError('업데이트 다운로드 크기 한도 초과입니다.')
    print(str(update(root,raw))+'개 프로그램 파일 업데이트 완료. start_windows.bat을 실행하세요.')
if __name__=='__main__':
    try:main()
    except Exception as exc:raise SystemExit('업데이트 실패. 기존 데이터는 유지됩니다. '+str(exc))
