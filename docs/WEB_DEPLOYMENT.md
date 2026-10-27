# 학교와 집에서 같은 패턴랩 사용하기

코드는 GitHub `baekitae/lab`, 실행은 Render에서 관리합니다. GitHub Pages에서는 Python 시세 서버를 실행할 수 없습니다.

## 처음 한 번: Render 배포

1. https://render.com/deploy?repo=https://github.com/baekitae/lab 에서 GitHub로 로그인합니다.
2. Render가 저장소를 요청하면 `baekitae/lab` 접근을 허용합니다. Blueprint로 `render.yaml`을 불러옵니다.
3. 표시된 웹 서비스와 영구 디스크의 **현재 가격을 확인**하고 배포를 진행합니다. 이 설정은 무료 플랜이 아닙니다. 영구 디스크 때문에 Starter 웹 서비스와 1GB 디스크를 사용합니다.
4. 서비스 상태가 Live가 되면 Render의 Environment에서 자동 생성된 `PL_WEB_PASSWORD` 값을 확인합니다. 다른 사람에게 공유하거나 GitHub에 넣지 않습니다. 로그인 ID는 `owner`입니다.
5. Render가 제공한 `https://…onrender.com/app` 주소에 접속해 로그인합니다. 이 주소를 학교와 집에서 동일하게 사용합니다.
6. 웹의 ① API 연결 설정에서 Gemini 키와 한국투자 키를 다시 등록하고 인증을 확인합니다. 기존 PC의 키·원장은 자동으로 이동하지 않습니다. 기존 데이터를 옮기려면 PC의 백업·복구에서 생성한 백업 ZIP을 웹에서 복원합니다. 키는 백업에 포함되지 않습니다. 브라우저에만 저장된 기존 보유·거래 화면의 데이터는 별도 내보내기·이관이 필요합니다.

Render에 직접 로그인하는 계정 권한은 이 채팅에 연결되어 있지 않습니다. GitHub 업로드만으로 Live 서비스가 생기지는 않습니다.

## 집에서 코드 수정

GitHub https://github.com/baekitae/lab 에 로그인하고 파일을 열어 연필 아이콘으로 수정 후 커밋합니다. 큰 수정은 저장소의 Code → Codespaces → Create codespace에서 할 수 있습니다. Codespaces의 사용 한도·요금은 GitHub에서 확인합니다. Render가 연결된 배포 브랜치(main)에 커밋되면 자동으로 재배포합니다. Render Events에서 성공 여부를 확인한 후 웹을 새로고침합니다. GitHub 파일 보기 주소와 실제 앱 주소는 다릅니다.

## 키와 데이터

- 웹 로그인 비밀번호와 암호화 마스터 키는 Render 환경 설정에 저장합니다. 코드에 넣지 않습니다.
- 웹 UI에서 등록한 API 키는 `PL_VAULT_MASTER_KEY`로 암호화해 영구 디스크에 저장합니다. 마스터 키를 변경·삭제하면 기존 키 저장소를 해독할 수 없습니다. 백업 ZIP에는 이 마스터 키와 API 키를 넣지 않습니다.
- 원장, 시세, 설정, 암호화된 키는 `/var/data`에 저장합니다. 서버 재배포로 지워지지 않도록 디스크를 유지합니다. 서비스/디스크 삭제는 별도입니다.
- 단일 소유자·단일 서버용 앱입니다. 웹 비밀번호를 공유하면 키 변경, 백업 다운로드, 데이터 수정 권한도 공유됩니다.
- 학교 네트워크가 Render 자체를 차단하면 학교에서는 웹 접속도 안 될 수 있습니다. 한국투자 API가 클라우드 서버의 접속을 허용하는지는 배포 후 인증·시세 갱신으로 별도 확인해야 합니다.
- 실제 주문 기능은 없습니다. 기존 Gemini 회의의 503 보류와 장기 성과 검증 미완료는 웹 배포로 해결되는 문제가 아닙니다.

## 배포 설정

`render.yaml`: Python 3.12.10, 싱가포르 단일 웹 서비스, `/healthz` 상태 확인, HTTPS 외부 주소 자동 인식. `/healthz` 외 앱·API·백업은 모두 로그인해야 접근할 수 있습니다. 로컬 실행은 기존대로 127.0.0.1 전용입니다. `PL_WEB_MODE=1`인데 HTTPS 주소·비밀번호·마스터 키·데이터 경로가 없으면 실행을 중단합니다. `PL_PUBLIC_ORIGIN`이 없으면 Render의 `RENDER_EXTERNAL_URL`을 사용합니다. 사용자 정의 도메인을 추가하면 `PL_PUBLIC_ORIGIN`도 해당 HTTPS 주소로 바꿉니다. 단일 프로세스로 실행해야 스케줄과 SQLite 원장이 공유됩니다.
