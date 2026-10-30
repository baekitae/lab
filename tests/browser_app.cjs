// Disposable full-app browser integration; never calls an external model.
const {chromium}=require('playwright'),fs=require('node:fs');
(async()=>{const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium'});try{
 const page=await browser.newPage({viewport:{width:1440,height:1000},timezoneId:'UTC'});const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:18772/app');await page.getByRole('button',{name:'① API 연결 설정',exact:true}).click();await page.locator('#globalModel').waitFor();
 await page.locator('#kisKey').fill('test-only-kis-appkey');await page.locator('#kisSecret').fill('test-only-kis-secret');
 await page.getByRole('button',{name:'한국투자 키만 저장',exact:true}).click();await page.waitForFunction(()=>document.getElementById('message').textContent.startsWith('한국투자 키를 저장했습니다.'));
 await page.locator('#kisSavedStatus').filter({hasText:'두 키가 등록되어 있습니다.'}).waitFor();
 if(await page.locator('#kisKey').inputValue()||await page.locator('#kisSecret').inputValue())throw Error('KIS fields not cleared');
 await page.getByRole('button',{name:'저장된 한국투자 키로 인증 확인',exact:true}).click();await page.waitForFunction(()=>document.getElementById('message').textContent.startsWith('한국투자 인증 성공.'));
 for(const role of ['TEAM_A','TEAM_B','TEAM_C','TEAM_D','MANAGER_EVAL','RED','DIRECTOR','AUDIT_AI'])await page.locator('#key-'+role).fill('test-user-secret-'+role);
 await page.locator('#globalModel').fill('gemini-test-model');await page.locator('#inputRate').fill('0');await page.locator('#outputRate').fill('0');await page.locator('#freeConfirmed').check();
 await page.getByRole('button',{name:'연결 설정과 키 저장',exact:true}).click();await page.getByText('설정과 키를 저장했습니다. 키는 다시 표시하지 않습니다. 모델 목록 조회와 실제 회의를 이어서 확인하세요.',{exact:true}).waitFor();
 if(await page.locator('input[type=password]').evaluateAll(es=>es.some(e=>e.value)))throw Error('Secret fields not cleared');
 await page.getByRole('button',{name:'등록된 키로 실제 모델 목록 불러오기',exact:true}).click();await page.getByText('모델 목록을 불러왔습니다. 실제 생성 호출은 아직 실행하지 않았습니다.',{exact:true}).waitFor();
 await page.locator('#modelSelect').selectOption('gemini-test-model');if(await page.locator('#globalModel').inputValue()!=='gemini-test-model')throw Error('Model selection not applied');
 await page.getByRole('button',{name:'② 시장 데이터',exact:true}).click();
 await page.locator('#connectorBasis').selectOption('split-adjusted');await page.locator('#connectorTerms').check();
 await page.locator('#enableConnector').click();await page.waitForFunction(()=>document.getElementById('message').textContent.startsWith('공급원을 활성화했습니다.'));
 if(await page.locator('#connectorBasis').inputValue()!=='split-adjusted'||!await page.locator('#connectorTerms').isChecked())throw Error('Saved connector config lost after save');
 await page.locator('#fetchConnector').click();await page.waitForFunction(()=>document.getElementById('message').textContent.includes('EGW00123'));
 if(!(await page.locator('#message').textContent()).includes('한국투자 과거 일봉 조회에 실패'))throw Error('Provider failure not actionable');
 await page.getByRole('button',{name:'① API 연결 설정',exact:true}).click();
 const fixture=await page.evaluate(()=>fetch('/__test__/fixtures').then(r=>r.json()));
 await page.screenshot({path:'/tmp/pattern-lab-validation/app-settings-fixture.png',fullPage:true});
 async function addFeed(provider,content,date){
  await page.getByRole('button',{name:'② 시장 데이터',exact:true}).click();
  await page.locator('#marketFile').setInputFiles({name:provider+'.csv',mimeType:'text/csv',buffer:Buffer.from(content)});
  await page.locator('#marketProvider').fill(provider);await page.locator('#marketAsOf').fill(date);await page.locator('#marketBasis').selectOption('unadjusted');await page.locator('#marketTerms').check();
  await page.getByRole('button',{name:'원본 보존 후 가져오기',exact:true}).click();await page.waitForFunction(()=>document.getElementById('message').textContent.startsWith('원본과 시세 피드를 저장했습니다:'));
 }
 async function compareLatest(){
  const state=await page.evaluate(()=>fetch('/api/app/state').then(r=>r.json()));const feeds=state.operations.feeds.slice(-2);
  await page.locator('#feedA').selectOption(feeds[0].id);await page.locator('#feedB').selectOption(feeds[1].id);
  await page.getByRole('button',{name:'최신 시세 정합성 검증',exact:true}).click();await page.getByText('출처 확인·가격 정합성 정책을 통과했습니다.',{exact:true}).waitFor();return feeds;
 }
 await addFeed('TEST_VENDOR_A',fixture.market_csv,'2026-10-26T21:00');if(!await page.locator('#compare').isDisabled())throw Error('Single-source comparison should be disabled');if(await page.locator('#feedB').inputValue())throw Error('Same provider offered as second source');await addFeed('TEST_VENDOR_B',fixture.market_csv,'2026-10-26T21:00');const base=await compareLatest();
 await page.goto('http://127.0.0.1:18772/ai');await page.waitForFunction(()=>document.getElementById('snapshot')?.options.length>0);await page.locator('#snapshot').selectOption(base[0].snapshot_id);
 await page.getByRole('button',{name:'Gemini 회의 시작',exact:true}).click();await page.getByText('12/12 단계',{exact:true}).waitFor({timeout:15000});
 await page.goto('http://127.0.0.1:18772/app');await page.getByRole('button',{name:'③ 모의거래·성적표',exact:true}).click();await page.locator('#conditionsChecked').check();
 await page.getByRole('button',{name:'익일 모의 주문 접수',exact:true}).click();await page.getByText('2026-10-27 익일 시가 모의 주문을 접수했습니다.',{exact:true}).waitFor();
 await page.evaluate(()=>fetch('/__test__/advance',{method:'POST'}));
 await addFeed('TEST_VENDOR_A',fixture.future_csv,'2026-10-28T21:00');await addFeed('TEST_VENDOR_B',fixture.future_csv,'2026-10-28T21:00');const future=await compareLatest();
 await page.getByRole('button',{name:'③ 모의거래·성적표',exact:true}).click();await page.locator('#settleSnapshot').selectOption(future[0].snapshot_id);await page.getByRole('button',{name:'후속 가격으로 모의 원장 정산',exact:true}).click();await page.getByText('2거래일의 모의 현금·수량·자산을 정산했습니다.',{exact:true}).waitFor();
 let state=await page.evaluate(()=>fetch('/api/app/state').then(r=>r.json()));if(state.operations.portfolio.marks.length!==2)throw Error('Missing daily portfolio marks');const teamA=state.operations.portfolio.scores.find(s=>s.book==='A');if(teamA.qty!==1||Math.abs(teamA.cash-(10000-45*1.001-1))>1e-8)throw Error('Wrong continuous cash/quantity');
 await page.screenshot({path:'/tmp/pattern-lab-validation/app-portfolio-fixture.png',fullPage:true});
 await page.getByRole('button',{name:'원장·기존 데이터',exact:true}).click();
 await page.locator('#ledgerFile').setInputFiles({name:'ledger.csv',mimeType:'text/csv',buffer:Buffer.from('date,symbol,side,qty,price,fees\n2026-01-02,SOXL,BUY,2,40,0\n')});
 await page.getByRole('button',{name:'원본 보존·원장 검산',exact:true}).click();await page.waitForFunction(()=>document.getElementById('message').textContent.startsWith('원본 보존 완료'));
 state=await page.evaluate(()=>fetch('/api/app/state').then(r=>r.json()));const ledger=state.operations.ledgers.at(-1);await page.locator('summary').filter({hasText:ledger.id.slice(0,12)}).click();
 await page.locator('#refs-'+ledger.id).fill(JSON.stringify({SOXL:{qty:2,avg:40,realized:0}}));await page.locator('#fees-'+ledger.id).fill('0');await page.locator('#full-'+ledger.id).check();await page.locator('#independent-'+ledger.id).check();
 await page.getByRole('button',{name:'증권사 기준과 대조·확정',exact:true}).click();await page.getByText('사용자 확인 전체 원장·증권사 대조를 기록했습니다.',{exact:true}).waitFor();
 await page.getByRole('button',{name:'승강 심사',exact:true}).click();await page.getByRole('button',{name:'새 평가 기준 버전 고정',exact:true}).click();await page.getByText('향후 평가에 적용할 기준 버전을 고정했습니다.',{exact:true}).waitFor();
 await page.getByRole('button',{name:'현재 표본으로 심사',exact:true}).click();await page.waitForFunction(()=>document.getElementById('message').textContent.startsWith('심사 유예:'));
 await page.getByRole('button',{name:'백업·복구',exact:true}).click();await page.getByRole('button',{name:'전체 데이터 백업 생성',exact:true}).click();await page.waitForFunction(()=>document.getElementById('message').textContent.startsWith('백업 생성 완료:'));
 const downloadPromise=page.waitForEvent('download');await page.getByRole('link',{name:/backup-/}).first().click();const download=await downloadPromise;await download.saveAs('/tmp/pattern-lab-validation/app-backup-fixture.zip');
 await page.locator('#restoreFile').setInputFiles('/tmp/pattern-lab-validation/app-backup-fixture.zip');await page.getByRole('button',{name:'복원 파일·해시 먼저 확인',exact:true}).click();await page.getByText('백업 파일·해시·SQLite 검사를 통과했습니다.',{exact:true}).waitFor();await page.locator('#restoreConfirmed').check();await page.getByRole('button',{name:'현재 상태 백업 후 복원',exact:true}).click();await page.waitForFunction(()=>document.getElementById('message').textContent.startsWith('복원 완료.'));
 await page.getByRole('button',{name:'운영 홈',exact:true}).click();await page.locator('#scheduleEnabled').check();await page.getByRole('button',{name:'자동 운영 설정 저장',exact:true}).click();await page.getByText('일일 회의 설정을 저장했습니다. 서버 실행 중에만 작동합니다.',{exact:true}).waitFor();
 await page.getByRole('button',{name:'지금 운영 조건 확인',exact:true}).click();await page.waitForFunction(()=>document.getElementById('message').textContent.includes('started'));
 await page.getByRole('button',{name:'긴급 중지',exact:true}).click();await page.getByText('긴급 중지했습니다. 현재 처리 중인 한 요청의 응답은 도착할 수 있습니다.',{exact:true}).waitFor();
 state=await page.evaluate(()=>fetch('/api/app/state').then(r=>r.json()));if(!state.operations.emergency.enabled)throw Error('Emergency state not persisted');
 await page.getByRole('button',{name:'원인 확인 후 중지 해제',exact:true}).click();await page.getByText('중지를 해제했습니다. 자동 회의·연결 설정을 다시 확인하세요.',{exact:true}).waitFor();
 await page.getByRole('button',{name:'② 시장 데이터',exact:true}).click();await page.locator('#connectorProvider').selectOption('yahoo');
 if(await page.locator('#connectorBasis').inputValue()!=='split-adjusted'||!await page.locator('#connectorBasis').isDisabled())throw Error('Yahoo basis not fixed');
 await page.locator('#connectorTerms').check();await page.locator('#enableConnector').click();await page.waitForFunction(()=>document.getElementById('message').textContent.startsWith('공급원을 활성화했습니다.'));
 await page.locator('#fetchConnector').click();await page.waitForFunction(()=>document.getElementById('message').textContent.startsWith('공급원 데이터 저장: yahoo'));
 state=await page.evaluate(()=>fetch('/api/app/state').then(r=>r.json()));if(!state.operations.feeds.some(f=>f.provider==='yahoo'&&f.price_basis==='split-adjusted'))throw Error('Yahoo feed not preserved');
 await page.locator('#disableConnector').click();await page.waitForFunction(()=>document.getElementById('message').textContent.startsWith('이 공급원의 자동 수집을 중지했습니다.'));
 state=await page.evaluate(()=>fetch('/api/app/state').then(r=>r.json()));if(state.connectors.find(c=>c&&c.provider==='yahoo').enabled)throw Error('Yahoo connector not disabled');
 await page.setViewportSize({width:390,height:844});
 for(const name of ['① API 연결 설정','② 시장 데이터','③ 모의거래·성적표','원장·기존 데이터','승강 심사','백업·복구']){
  await page.getByRole('button',{name,exact:true}).click();if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Mobile overflow: '+name);
 }
 await page.getByRole('button',{name:'① API 연결 설정',exact:true}).click();await page.screenshot({path:'/tmp/pattern-lab-validation/app-mobile-fixture.png',fullPage:true});
 if(errors.length)throw Error(errors.join('\n'));
 console.log('PASS disposable full app: visible key registration, secret masking, model list, dual CSV validation, 12-stage fixture meeting, continuous 2-day portfolio, ledger reconciliation, deferred promotion, backup/restore, scheduler, mobile tabs and no JavaScript errors.');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1});
