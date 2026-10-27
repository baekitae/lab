"""One-click local setup with pinned dependencies and visible errors."""
import hashlib,json,os,subprocess,sys,urllib.request,venv,webbrowser
from pathlib import Path

def ensure_pip(python):
    """Repair an interrupted venv setup before trying to install dependencies."""
    probe=subprocess.run([str(python),'-m','pip','--version'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if probe.returncode:
        print('가상환경의 pip를 복구합니다. 기존 데이터는 유지됩니다.',flush=True)
        subprocess.run([str(python),'-m','ensurepip','--upgrade'],check=True)
        subprocess.run([str(python),'-m','pip','--version'],check=True)

def main():
    if sys.version_info<(3,12):raise SystemExit('Python 3.12 이상을 설치하세요. Windows 설치 시 Add Python to PATH를 선택하세요.')
    root=Path(__file__).resolve().parent;os.chdir(root)
    try:
        with urllib.request.urlopen('http://127.0.0.1:8770/api/app/state',timeout=2) as r:state=json.load(r)
        if state.get('release')=='2.0.0':
            webbrowser.open('http://127.0.0.1:8770/app');return 0
    except Exception:pass
    env=root/'.venv';python=env/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    if not python.exists():
        print('개발 환경을 준비합니다. 첫 실행은 인터넷 연결이 필요합니다.',flush=True)
        venv.EnvBuilder(with_pip=True).create(env)
    ensure_pip(python)
    required=(root/'requirements.txt').read_bytes();fingerprint=hashlib.sha256(required+str(sys.version_info[:2]).encode()).hexdigest()
    marker=env/'pattern-lab-requirements.sha256'
    if not marker.exists() or marker.read_text()!=fingerprint:
        print('고정 버전 거래일 캘린더와 의존성을 설치합니다.',flush=True)
        subprocess.run([str(python),'-m','pip','install','--disable-pip-version-check','-r',str(root/'requirements.txt')],check=True)
        marker.write_text(fingerprint)
    return subprocess.call([str(python),'-u',str(root/'server.py')],cwd=root)
if __name__=='__main__':
    try:raise SystemExit(main())
    except subprocess.CalledProcessError:raise SystemExit('설치에 실패했습니다. 인터넷·프록시 연결과 위 오류를 확인한 뒤 다시 실행하세요.')
