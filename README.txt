Pattern Lab v1.3.3 — 예측 기록 및 워크포워드 백테스트 개선판

- 개별 매매 기록 수정 및 삭제
- CSV 중복 감지 및 수동 저장 중복 차단
- 거래 표의 체크박스로 여러 건 선택
- 전체 선택 및 선택 항목 일괄 삭제
- 현재 종목 탭의 매매 기록 전체 삭제
- 삭제 전 확인창 제공. 삭제는 되돌릴 수 없으므로 필요 시 먼저 보유 정보 백업을 하세요.
- 전체 삭제는 현재 종목 탭에만 적용되며 다른 종목 기록은 유지됩니다.

Windows: start_windows.bat 실행. 기존 서버를 종료하고 새 폴더에 압축을 풀어 실행하세요.

추가 수정: 키움증권 주문/체결 합본 CSV 지원. CP949/EUC-KR 인코딩, YYYY/MM/DD 날짜, 주문행+체결 상세행 쌍을 인식합니다. 미체결(체결수량 또는 체결단가가 0인) 주문은 거래로 가져오지 않습니다.


v1.2 추가 기능
- 종목별 워크포워드 백테스트: 과거 각 시점에서 미래 결과가 이미 확인된 사례만 유사 사례 후보로 사용합니다.
- 방향 분류 정확도, 시장 국면별 적중률, 평균 수익률, 가정 비용 반영 전략 누적 수익률, 최대 낙폭을 표시합니다.
- 평가 간격 및 단순 왕복 거래 비용 가정을 선택할 수 있습니다. 기본 평가 간격은 10거래일이며 예측 기간 설정과 별도로 조정할 수 있습니다.
- 기존 KIS API 시세 조회, 종목·매매 기록·예측 저장 형식은 변경하지 않았습니다. 백테스트는 브라우저에서 실행되며 결과를 자동 저장하지 않습니다.
- 백테스트 결과는 과거 데이터 기반의 모의 검증이며 투자 성과를 보장하지 않습니다.


Pattern Lab v1.3.3 changes
- Prediction records now retain analysis configuration (lookback, horizon, similar-case count, flat threshold).
- The same security/date/horizon can be saved again when the analysis configuration differs.
- Saved prediction records support note editing and deletion.
- Walk-forward backtest startup threshold lowered to 75 daily rows for preliminary checks; 160+ rows remains recommended for more stable evaluation.
- This is not evidence of investment performance. Validate against real KIS daily bars and inspect sample count, overlapping horizons, costs, and out-of-sample metrics.

문제 해결: 이 버전은 이전 버전과 포트 충돌을 피하기 위해 127.0.0.1:8767을 사용합니다.


[v1.3.4 수정사항]
- 예측 기록 저장 시 분석 기간 설정을 실제 화면의 historyDays 선택값에서 읽도록 수정했습니다.
- 분석 기간(3년/5년), 예측 기간, 유사 사례 수, 보합 기준이 중복 판정에 반영되어 서로 다른 설정을 별도 저장할 수 있습니다.


[v1.3.4 설정 보존 보완]
- 분석 실행 당시의 분석 기간, 예측 기간, K, 보합 기준을 분석 결과 객체에 함께 기록합니다.
- 예측 저장 시 화면의 초기화된 선택값 대신 해당 분석 결과의 설정을 저장합니다.
- 분석 결과를 다시 표시할 때 해당 설정을 선택 상자에 복원합니다.
- 저장된 예측의 '조회' 버튼에서 설정과 통계 결과를 확인할 수 있습니다.
- 기존 예측 기록에 분석 기간이 저장되지 않았다면 임의로 복구하지 않고 '미상'으로 표시합니다.
- 기존 백테스트 계산식은 이번 수정에서 변경하지 않았습니다.
