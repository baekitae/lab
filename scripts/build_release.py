"""Build a source release without credentials, ledgers, caches or virtual environments."""
import hashlib,json,os,sys,zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
output=Path(sys.argv[1]) if len(sys.argv)>1 else ROOT.parent/'pattern_lab_ai_league_v2_0.zip'
files=[p for p in ROOT.iterdir() if p.is_file() and (p.suffix in {'.py','.html','.bat','.sh','.txt','.md'} or p.name in {'.gitignore','.gitattributes','render.yaml'})]
for directory,extensions in [('docs',{'.md'}),('tests',{'.py','.cjs'}),('scripts',{'.py'})]:
    files.extend(p for p in (ROOT/directory).rglob('*') if p.is_file() and p.suffix in extensions and '__pycache__' not in p.parts)
entries={}
for p in sorted(files):
    if p.is_symlink():raise SystemExit('Release source symlink is not permitted.')
    entries[p.relative_to(ROOT).as_posix()]=p.read_bytes()
entries['data/.keep']=b''
# Check against injected runtime secrets without printing or exporting their values.
values=[v.encode() for k,v in os.environ.items() if k.startswith('PL_') and k.endswith('_API_KEY') and len(v)>=8]
if any(secret in content for secret in values for content in entries.values()):raise SystemExit('Sensitive runtime binding detected in release source; export stopped.')
manifest={'release':'2.0.0','credentials_included':False,'data_included':False,
          'files':{name:hashlib.sha256(raw).hexdigest() for name,raw in entries.items()}}
entries['RELEASE_MANIFEST.json']=json.dumps(manifest,ensure_ascii=False,indent=2).encode()
output.parent.mkdir(parents=True,exist_ok=True)
with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
    for name,raw in entries.items():
        info=zipfile.ZipInfo('pattern_lab_ai_league_v2_0/'+name,(2026,10,27,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
        info.external_attr=(0o100755 if name.endswith('.sh') else 0o100644)<<16
        archive.writestr(info,raw)
with zipfile.ZipFile(output) as archive:
    if archive.testzip():raise SystemExit('ZIP integrity check failed.')
    for name,expected in manifest['files'].items():
        if hashlib.sha256(archive.read('pattern_lab_ai_league_v2_0/'+name)).hexdigest()!=expected:raise SystemExit('Release manifest check failed.')
print(json.dumps({'path':str(output),'files':len(entries),'bytes':output.stat().st_size,'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'credentials_included':False},ensure_ascii=False))
