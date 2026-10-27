# 2.0 개발·운영 구조

사용 방법은 [README](../README.md), 실제 검증 상태는 [출시 점검표](RELEASE_CHECKLIST.md)를 우선 확인하세요.

## 모듈

| 모듈 | 책임 |
|---|---|
| `bootstrap.py`, 실행 스크립트, `requirements.txt` | 로컬 venv·고정 의존성·서버 시작 |
| `settings_store.py` | 공개 설정과 키 분리, Windows DPAPI, 다른 OS의 세션 메모리, 원본 보존 복구 |
| `providers.py` | 실제 Gemini REST, 모델 목록, 구조화 출력·사용량, TLS 검증, 자격 증명 리다이렉트 금지 |
| `ai_league.py` | 12단계·공개 범위·후보 판단·재심·취소·예산·불변 이벤트·전망 채점 |
| `league.py`, `league.html` | 키 없는 기준 전략과 시장 스냅샷 저장 |
| `ledger_formats.py` | 정규화/한국어/키움 2행·3행 원본 해석 |
| `operations.py` | 출처/정합성, 개인 원장 감사, 연속 모의 원장, 심사·스케줄·긴급 중지 |
| `trading_calendar.py` | XNYS 실제 거래일·폐장·DST |
| `connectors.py` | 사용자가 승인한 KIS/Stooq 읽기 전용 수집 |
| `paper.py` | 기간별 독립 가상 체결 |
| `backups.py` | SQLite online backup·ZIP 해시·DB/이벤트 검사·원상 복구 |
| `app.html` | API 입력을 포함한 통합 운영 UI |
| `server.py` | loopback HTTP API와 기존 패턴랩 연동 |

## 키 설정

클라우드/OS 보안 환경 값은 로컬 입력보다 우선하며 UI에서 덮어쓰지 않습니다. 값이 없는 로컬 역할은 통합 UI에서 입력합니다.

역할 변수: `PL_TEAM_A_API_KEY`, `PL_TEAM_B_API_KEY`, `PL_TEAM_C_API_KEY`, `PL_TEAM_D_API_KEY`, `PL_MANAGER_EVAL_API_KEY`, `PL_RED_API_KEY`, `PL_DIRECTOR_API_KEY`, `PL_AUDIT_AI_API_KEY`.

공통 비밀 아닌 설정: `PL_GEMINI_MODEL`, `PL_INPUT_KRW_PER_MILLION`, `PL_OUTPUT_KRW_PER_MILLION`, `PL_MONTHLY_BUDGET_KRW`. 로컬 예산 기본 상한 20,000원.

역할별 선택 설정: `PL_{역할}_MODEL`, `PROJECT_ID`, `RPM`, `TPM`, `RPD`. 빈 개별 모델은 공통 모델로 해석합니다. 비워 둔 개별 한도는 보수적인 전역 로컬 제한(`PL_LOCAL_RPM=2`, `PL_LOCAL_TPM=100000`, `PL_LOCAL_RPD=12`)을 사용합니다. 이것은 공급자의 실제 무료 한도가 아닙니다.

키 값은 GET 응답, 원장, 모델 입력, 브라우저 localStorage, 로그, JSON 내보내기, 배포 ZIP과 앱 백업에서 제외됩니다. Windows 암호화 파일과 복구 보존본은 현재 OS 사용자 계정에 묶입니다. 다른 OS에서는 입력 키를 평문 파일로 대체 저장하지 않습니다.

## AI·비용 계약

A/B/C/D 독립 초안에는 같은 시장 자료와 자기 역할만 전달합니다. 반박 단계는 네 확정 초안만 보며 먼저 작성된 수정안은 보지 않습니다. 심사 프로젝트는 공통 원안·수정안을 보고 정규 판단과 그림자 후보의 수량·전망·취소 조건을 비교합니다. 레드팀·총괄·감사는 정식 수정 보고서를 순차 검토합니다.

계산·인용·수량·기간·확률·현금·위험 규칙의 최종 판정은 Python이 수행합니다. AI 승인 의견과 실제 사용자 전략의 게이트는 별도이며, 원장 G1 또는 최신 이중 시세가 미통과면 승인하지 않습니다. 원장 게이트는 사용자 확인 전체 이력·별도 검산/증권사 대조를 기록한 정책 판정입니다.

실제 생성 HTTP 요청은 재시도 포함 회의당 최대 12회. 429는 같은 역할에서 2초 백오프 후 한 번만 재시도하며 다른 키로 우회하지 않습니다. 추가 요청 때문에 12회가 소진되면 후속 단계는 보류됩니다. 재심은 명시적으로 최대 두 번입니다. 네트워크 응답 불명은 자동 재전송하지 않습니다.

요청 전 보수적 비용을 원자적으로 예약합니다. 실패/응답 불명 예약액은 유지합니다. 알려진 추정 비용이 예약보다 높으면 차액을 기록합니다. 설정 단가가 실제 단가·환율과 다르거나 외부 사용량/청구 지연이 있으면 결제 보증이 되지 않습니다. 여러 모델을 쓰면 모든 모델 중 최고 단가를 공통 비용 단가로 사용하세요.

Gemini 3 이상은 공식 ThinkingConfig의 LOW를 사용하고 생성 요청 제한 시간은 180초입니다. 현재 모델별 지원과 실제 응답 상태를 확인해야 합니다. 테스트 응답을 운영 대체 제공자로 활성화하는 옵션은 없습니다.

## 모의 체결과 사후 평가

기간별 전망 평가와 연속 운용 원장은 별도입니다. 기간별 실험은 각각 $10,000으로 시작하며 비중첩 심사용 표본만 따로 선택합니다. 상승 Brier 사건은 `horizon_close > cutoff_close`, 방향 분류 보합은 ±0.2%입니다.

연속 원장은 검토한 목표 수량을 다음 NYSE 세션 시가에 가정 체결합니다. 편도 수수료 $1, 슬리피지 0.1%, 롱/현금만 허용합니다. 매수의 목표 노출은 초기 $1,000 및 현재 자산 10% 중 작은 값으로 제한합니다. 가격 갭으로 초과하면 거부하며 모델 수량을 몰래 바꾸지 않습니다. 일별 종가 기준 자산으로 MDD를 계산합니다. 장중 MDD는 아닙니다.

두 공급원 정합성은 실제 출처/조정 정책에 대한 사용자의 확인과 가격 차이·거래일·신선도 검사입니다. 출처 자체를 외부 공증하지 않습니다. 과거 정산 가격이 달라지면 정산을 보류합니다. 휴장 및 조기 폐장 캘린더의 지원 범위는 2000~2035이며 범위 밖은 업데이트 전 보류해야 합니다.

## 실행과 API

일반 실행: `python3 bootstrap.py`. 개발 환경 준비 후 직접 실행: `.venv/bin/python -u server.py`.

- `/app`: 통합 홈·키 입력·시장/원장/운영/복구
- `/ai`: 실제 Gemini 회의·보고서·기간별 사후 평가·JSON
- `/league`: 키 없는 기준 전략·합성 표본
- `/`: 기존 보유·거래 화면

`GET /api/app/state`는 비밀 값 없는 상태와 CSRF 토큰을 제공합니다. `/api/app/*`의 POST는 로컬 Host/Origin, JSON, `X-Pattern-Lab-Token`을 검사합니다. 하위 작업은 `settings`, `settings/recover`, `models`, `market/csv`, `market/compare`, `connector/config`, `connector/fetch`, `paper/queue`, `paper/settle`, `ledger/import`, `ledger/review`, `ledger/confirm`, `migration/preview`, `migration/commit`, `promotion/rules`, `promotion/review`, `promotion/apply`, `schedule`, `schedule/tick`, `emergency`, `meeting/cancel`, `backup/create`, `backup/restore`, `replay`입니다.

기존 AI API `/api/ai`, `/api/ai/runs`, `/api/ai/run`, `/api/ai/evaluations`도 유지합니다. 앱은 localhost에만 바인딩하며 공개 호스트로 노출하는 구성은 제공하지 않습니다.

## 저장·복구

`data/league.sqlite3`, `data/ai_league.sqlite3`, `data/operations.sqlite3`. 각 DB의 로그와 원본은 정상 경로에서 UPDATE/DELETE를 차단합니다. 해시 체인과 비용/채점 원장을 대조합니다. DB 소유자의 전체 이력 재작성·끝부분 삭제를 막는 외부 공증은 아닙니다.

백업은 자격 증명을 제외한 DB와 설정만 포함합니다. 복원 전 SHA-256, SQLite integrity_check, 이벤트/스냅샷 참조를 검사합니다. 진행 중 회의가 있으면 복원하지 않습니다. 복원 전 상태를 별도로 백업합니다. 파일 교체 실패 시 원래 파일로 되돌립니다. 복원 과정은 API로 직렬화되며 외부에서 DB를 직접 변경하는 동작은 지원하지 않습니다.

환경 게시/스냅샷은 실행 중 프로세스를 보존하지 않습니다. 새 작업에서는 서버를 재시작하고 현재 주입된 인증을 재사용합니다. 별도 Git worktree가 필요하지 않습니다.
