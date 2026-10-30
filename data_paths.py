"""Keep Windows user data outside versioned ZIP folders; preserve legacy data."""
import os,shutil,sqlite3,tempfile
from pathlib import Path
from contextlib import closing

def data_directory(root,env=None,platform=None):
    env=os.environ if env is None else env
    platform=os.name if platform is None else platform
    if env.get('PL_DATA_DIR'):return Path(env['PL_DATA_DIR']).expanduser()
    if env.get('PL_WEB_MODE')=='1':raise ValueError('웹 실행에는 PL_DATA_DIR이 필요합니다.')
    if platform=='nt':
        if not env.get('LOCALAPPDATA'):raise ValueError('Windows 사용자 데이터 경로를 찾지 못했습니다.')
        return Path(env['LOCALAPPDATA'])/'PatternLab'/'data'
    return Path(root)/'data'

def migrate_legacy(source,target):
    """First migration only. Never merge stores, overwrite a target or delete originals."""
    source,target=Path(source),Path(target)
    if source.resolve()==target.resolve() or not source.exists():return False
    if target.exists():return False
    files=[p for p in source.iterdir() if p.name!='.keep']
    if not files:return False
    target.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.pattern-lab-migrate-',dir=target.parent))
    try:
        for item in files:
            if item.is_symlink():raise ValueError('데이터에 바로가기가 있습니다. 원본을 보존하고 수동 복구하세요.')
            if item.name.endswith(('-wal','-shm')):continue
            dest=stage/item.name
            if item.suffix=='.sqlite3':
                with closing(sqlite3.connect(item.resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(dest)) as dst:
                    src.backup(dst)
                    if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('기존 원장 무결성 검사 실패입니다.')
            elif item.is_dir():
                if any(p.is_symlink() for p in item.rglob('*')):raise ValueError('데이터에 바로가기가 있습니다.')
                shutil.copytree(item,dest)
            else:shutil.copy2(item,dest)
        # rename refuses an existing nonempty store; never replace its contents.
        if target.exists():return False
        stage.rename(target)
        return True
    finally:
        if stage.exists():shutil.rmtree(stage)

def application_data(root):
    target=data_directory(root)
    if os.name=='nt' and not os.environ.get('PL_DATA_DIR'):
        migrate_legacy(Path(root)/'data',target)
    return target
