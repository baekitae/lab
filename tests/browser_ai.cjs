// Run against tests/serve_ai_fixture.py --test-only. This never validates live Gemini.
const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium'});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:18771/ai');await page.waitForSelector('#roles .role');
  const snapshots=await page.evaluate(()=>fetch('/api/league').then(r=>r.json()).then(d=>d.snapshots));
  const base=snapshots.find(s=>s.count===80),future=snapshots.find(s=>s.count===100);
  await page.locator('#snapshot').selectOption(base.id);
  await page.getByRole('button',{name:'Gemini 회의 시작',exact:true}).click();
  await page.getByText('12/12 단계',{exact:true}).waitFor({timeout:15000});
  if(await page.locator('.step.done').count()!==12)throw Error('Incomplete stages');
  await page.locator('#snapshot').selectOption(future.id);
  await page.getByRole('button',{name:'후속 가격으로 AI 전망 채점',exact:true}).click();
  await page.getByText('AI 전망 4기간 채점 · 미래 가격 부족 없음',{exact:true}).waitFor();
  await page.getByText('1거래일 · 2025-03-23 · synthetic',{exact:true}).click();
  const downloadPromise=page.waitForEvent('download');
  await page.getByRole('button',{name:'회의 JSON 내보내기',exact:true}).click();
  const download=await downloadPromise;
  if(!download.suggestedFilename().startsWith('pattern-lab-ai-'))throw Error('Missing JSON export');
  await page.screenshot({path:'/tmp/pattern-lab-validation/ai-test-fixture-desktop.png',fullPage:true});
  await page.reload();await page.getByText('12/12 단계',{exact:true}).waitFor();
  await page.setViewportSize({width:390,height:844});
  await page.locator('.report').first().locator('summary').first().click();
  if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Mobile overflow');
  await page.screenshot({path:'/tmp/pattern-lab-validation/ai-test-fixture-mobile.png',fullPage:true});
  if(errors.length)throw Error(errors.join('\n'));
  const runs=await page.evaluate(()=>fetch('/api/ai').then(r=>r.json()).then(d=>d.runs));
  if(runs.length!==1||runs[0].outcomes.length!==4)throw Error('Repeated execution duplicated results');
  console.log('PASS explicit test fixture: async 12-stage pipeline, 4 horizon scores, JSON export, persistence, no duplicate results, mobile layout and no JavaScript errors. Live Gemini remains unverified.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
