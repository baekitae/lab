"""Application backups exclude credentials and preserve the pre-restore state."""
import hashlib,json,sqlite3,tempfile,uuid,zipfile
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path
from settings_store import atomic_write

DBS=('league.sqlite3','ai_league.sqlite3','operations.sqlite3')
ALLOWED=set(DBS)|{'settings.json'}

class Backups:
    def __init__(self,data):self.data=Path(data);self.directory=self.data/'backups';self.directory.mkdir(parents=True,exist_ok=True)
    def create(self):
        name='backup-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-'+uuid.uuid4().hex[:8]+'.zip'
        path=self.directory/name
        with tempfile.TemporaryDirectory() as folder:
            entries={}
            for name_db in DBS:
                source=self.data/name_db
                if not source.exists():continue
                copy=Path(folder)/name_db
                with closing(sqlite3.connect(source)) as src,closing(sqlite3.connect(copy)) as target:src.backup(target)
                entries[name_db]=copy.read_bytes()
            if (self.data/'settings.json').exists():entries['settings.json']=(self.data/'settings.json').read_bytes()
            if sum(len(v) for v in entries.values())>200000000:raise ValueError('앱 내 백업 크기 한도 200MB를 초과했습니다. SQLite 온라인 백업을 별도로 보존하세요.')
            manifest={'format':'pattern-lab-backup-v1','created_at':datetime.now(timezone.utc).isoformat(),
                      'credentials_included':False,'files':{n:hashlib.sha256(raw).hexdigest() for n,raw in entries.items()}}
            with zipfile.ZipFile(path,'x',zipfile.ZIP_DEFLATED) as z:
                for n,raw in entries.items():z.writestr(n,raw)
                z.writestr('manifest.json',json.dumps(manifest))
        return {'name':path.name,'bytes':path.stat().st_size,'manifest':manifest}
    def inspect(self,raw):
        import io,stat
        if len(raw)>100_000_000:raise ValueError('백업은 100MB 이하여야 합니다.')
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                infos=z.infolist();names=[i.filename for i in infos]
                if len(set(names))!=len(names) or set(names)-ALLOWED-{'manifest.json'} or 'manifest.json' not in names:raise ValueError('허용되지 않은 백업 파일 목록입니다.')
                if sum(i.file_size for i in infos)>200_000_000 or any(stat.S_ISLNK(i.external_attr>>16) for i in infos):raise ValueError('백업 압축 해제 크기가 너무 크거나 링크가 포함되었습니다.')
                manifest=json.loads(z.read('manifest.json'));entries={n:z.read(n) for n in names if n!='manifest.json'}
                if manifest.get('format')!='pattern-lab-backup-v1' or manifest.get('credentials_included') is not False or set(manifest['files'])!=set(entries):raise ValueError('백업 형식이 올바르지 않습니다.')
                for n,content in entries.items():
                    if hashlib.sha256(content).hexdigest()!=manifest['files'][n]:raise ValueError('백업 해시가 일치하지 않습니다.')
                if set(DBS)-set(entries):raise ValueError('세 데이터베이스가 모두 필요합니다.')
                with tempfile.TemporaryDirectory() as folder:
                    for n,content in entries.items():
                        if n.endswith('.sqlite3'):
                            p=Path(folder)/n;p.write_bytes(content)
                            with closing(sqlite3.connect(f'file:{p}?mode=ro',uri=True)) as db:
                                if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('SQLite 무결성 검사 실패입니다.')
                    from league import League
                    from ai_league import AILeague
                    from operations import Operations
                    local_league=League(Path(folder)/'league.sqlite3')
                    local_ai=AILeague(Path(folder)/'ai_league.sqlite3',local_league)
                    try:
                        local_ops=Operations(Path(folder)/'operations.sqlite3',local_league,local_ai)
                        if not local_ops.verify()['valid']:raise ValueError('운영 이벤트 해시 검증에 실패했습니다.')
                        local_ops.replay()
                        with local_ai.db() as db:run_rows=db.execute('SELECT id,snapshot_id FROM ai_run').fetchall()
                        for row in run_rows:
                            if not local_ai.verify(row['id']):raise ValueError('AI 이벤트 해시 검증에 실패했습니다.')
                            local_ai.get_snapshot(row['snapshot_id'])
                        for feed in local_ops.events('feed'):local_ops.snapshot(feed['snapshot_id'])
                    finally:local_ai.shutdown()
                if 'settings.json' in entries:
                    from settings_store import PUBLIC_FIELDS
                    settings=json.loads(entries['settings.json'])
                    if not isinstance(settings,dict) or set(settings)-PUBLIC_FIELDS or any(not isinstance(v,str) or len(v)>200 or any(ord(c)<32 for c in v) for v in settings.values()):raise ValueError('백업에 허용되지 않은 설정이 있습니다.')
                return entries,manifest
        except (zipfile.BadZipFile,KeyError,json.JSONDecodeError,sqlite3.Error):raise ValueError('유효한 패턴랩 백업 ZIP이 아닙니다.') from None
    def restore(self,raw,confirm=False):
        entries,manifest=self.inspect(raw)
        if confirm is not True:return {'dry_run':True,'files':list(entries),'manifest':manifest}
        before=self.create();old={n:(self.data/n).read_bytes() if (self.data/n).exists() else None for n in entries}
        try:
            for n,content in entries.items():atomic_write(self.data/n,content)
        except Exception:
            for n,content in old.items():
                if content is not None:atomic_write(self.data/n,content)
                elif (self.data/n).exists():(self.data/n).unlink()
            raise ValueError('복원에 실패했습니다. 변경 전 백업은 보존되었습니다.') from None
        return {'restored':True,'before_restore_backup':before['name'],'manifest':manifest,'credentials_changed':False}
    def list(self):
        return [{'name':p.name,'bytes':p.stat().st_size} for p in sorted(self.directory.glob('backup-*.zip'),reverse=True)]
