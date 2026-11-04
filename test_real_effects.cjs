const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const {spawn}=require('node:child_process'),{createInterface}=require('node:readline');
const dep=process.env.MEETING_REACT_TEST_MODULES;
if(!dep)throw new Error('Set MEETING_REACT_TEST_MODULES to existing React/ReactDOM/jsdom node_modules (no install required).');
const {JSDOM}=require(dep+'jsdom');
const dom=new JSDOM('<div id="root"></div>',{url:'http://offline.invalid'});
Object.assign(global,{window:dom.window,document:dom.window.document,IS_REACT_ACT_ENVIRONMENT:true});
const React=require(dep+'react'),{createRoot}=require(dep+'react-dom/client'),{act}=React;
const home=fs.mkdtempSync(require('node:path').join(process.env.TMPDIR, 'meeting-real-effects-'));fs.chmodSync(home,0o700);
const child=spawn(process.env.PYTHON||'python',['-B','-u',__dirname+'/real_effect_test_backend.py'],{env:{HOME:home,HERMES_HOME:home,TMPDIR:home,PATH:process.env.PATH,PYTHONDONTWRITEBYTECODE:'1'},stdio:['pipe','pipe','pipe']});
let id=0,stderr='';const waiting=new Map(),traffic=[];
child.stderr.on('data',x=>stderr+=x);
const exit=new Promise(resolve=>child.on('exit',resolve));
createInterface({input:child.stdout}).on('line',line=>{const r=JSON.parse(line);waiting.get(r.id)(r);waiting.delete(r.id)});
const rpc=data=>new Promise(resolve=>{waiting.set(++id,resolve);child.stdin.write(JSON.stringify({...data,id})+'\n')});
let holdWrite=false,heldWrite=null,releaseWrite,failStatus=false;
let holdStatus=false,heldStatus=null,releaseStatus;
let answerFault=process.env.ANSWER_FAULT||'', pollFault=false, queryFault=false, refreshFault=false;
const timers=new Map();let timerId=0;
async function rest(path,options={}){
 if(failLifecycle && path===failLifecycle)throw new Error('synthetic-sensitive-lifecycle');
 if(failReadback&&path==='/credentials/meta'&&!options.method)throw new Error('synthetic-sensitive-readback');
 if(failStatus&&path==='/status')throw new Error('synthetic-sensitive-status');
 if(answerFault==='cancel' && path==='/assist/cancel')throw new Error('synthetic-sensitive-cancel\nTraceback: synthetic-stack');
 if(['missing','legacy-missing'].includes(answerFault) && path.startsWith('/assist/'))await rpc({control:'drop-answers'});
 if(answerFault.startsWith('legacy')){
  path=path.split('?')[0];
  if(options.body){options={...options,body:{...options.body}};delete options.body.response_contract;}
 }
 const r=await rpc({path,...options});traffic.push({path,method:options.method||'GET',code:r.code});
 if(['lost','replay','lost-live'].includes(answerFault) && path==='/assist')throw new Error('synthetic-sensitive-answer-transport');
 if(pollFault && path.startsWith('/assist/'))throw new Error(answerFault==='poll'?'synthetic-sensitive-poll-404 not found':'synthetic-sensitive-poll-transport');
 if(process.env.ACTION_TRANSPORT_FAULT===path)throw new Error('synthetic-sensitive-action-transport');
 if(holdWrite&&path==='/credentials/meta'&&options.method){heldWrite=r;await new Promise(resolve=>releaseWrite=resolve);}
 if(holdStatus&&path==='/status'){heldStatus=r;await new Promise(resolve=>releaseStatus=resolve);}
 if(r.code>=400)throw new Error('HTTP '+r.code+' '+JSON.stringify(r.body));return r.body;
}
let failReadback=false, failLifecycle=process.env.INITIAL_LIFECYCLE_FAULT ? (process.env.INITIAL_LIFECYCLE_FAULT==='claim'?'/overlay/claim':'/overlay/lifecycle') : '';
let status={capabilities:{macos_transcription:true,overlay:true,meta_asr:true}},registered;
const ctx={rest,register:c=>{if(c.area==='panes')registered=c;return()=>{}}};
const source=fs.readFileSync(__dirname+'/desktop/plugin.js','utf8').replace(/^import .*;\r?\n/gm,'').replace('export default','const plugin =');
const store=new Map([['meeting-copilot.scope.v2',JSON.stringify({backend:'macos',transcriptionProvider:'meta'})]]);
const box={...React,h:React.createElement,ctx,crypto:require('node:crypto').webcrypto,console,window:dom.window,
Button:'button',Input:'input',Textarea:'textarea',Badge:'span',PALETTE_AREA:'palette',setInterval:fn=>{timers.set(++timerId,fn);return timerId},clearInterval:id=>timers.delete(id),setTimeout,clearTimeout,
host:{notify:()=>{},paneVisibility:()=>({get:()=>true}),revealPane:()=>{}},useValue:a=>a.get(),useQueryClient:()=>({invalidateQueries:async()=>{if(refreshFault)throw new Error('synthetic-sensitive-status-refresh');}}),
useQuery:q=>({error:q.queryKey[1]==='status'&&queryFault?new Error('synthetic-sensitive-status-query\nTraceback: synthetic-stack'):null,data:q.queryKey[1]==='status'?status:q.queryKey[1]==='models'?{providers:[]}:[]}),
localStorage:{getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v)}};
box.globalThis=box;window.confirm=()=>true;
const unregister=vm.runInNewContext(source+'\nplugin.register(ctx)',box);
const root=createRoot(document.getElementById('root'));
const ticks=async()=>{for(let i=0;i<8;i++)await new Promise(r=>setImmediate(r));};
async function flush(){for(let i=0;i<100;i++){await act(async()=>{await ticks();await rpc({path:'/review/probe'});await ticks()});if(!document.querySelector('[aria-label="Meta API key"]')?.disabled&&waiting.size===0)return;}throw new Error('UI did not settle');}
async function render(){await act(async()=>{root.render(process.env.STRICT?React.createElement(React.StrictMode,null,registered.render()):registered.render());await ticks()});await flush()}
async function refresh(){status=await rest('/status');await render()}
const button=label=>[...document.querySelectorAll('button')].find(e=>e.textContent===label);
async function click(label){const b=button(label);assert.ok(b,label);assert.equal(b.disabled,false,label+' disabled');await act(async()=>{b.click();await ticks()});await flush()}
async function until(check){for(let i=0;i<100;i++){await act(async()=>{await ticks();await rpc({path:'/review/probe'});await ticks()});if(check())return;}throw new Error('gate did not settle');}
async function clickPending(label){const b=button(label);assert.ok(b,label);assert.equal(b.disabled,false,label+' disabled');await act(async()=>{b.click();await ticks()});}
async function key(value){const field=document.querySelector('[aria-label="Meta API key"]');await act(async()=>{Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set.call(field,value);field.dispatchEvent(new window.Event('input',{bubbles:true}));});}
const probe=()=>rest('/review/probe');
const visible=e=>{for(let n=e;n;n=n.parentElement)if(n.tagName==='DETAILS'&&!n.open)return false;return true;};
const reports=[];
(async()=>{
 if(process.env.CLAIM_CONFLICT){
  const ticket=await rest('/overlay/lifecycle');
  const claim=await rest('/overlay/claim',{method:'POST',body:{...ticket,client:'other-synthetic-renderer',creation:1}});
  assert.ok(claim.pane);
 }
 await render();
 if(process.env.ANSWER_FAULT && ['status-query','engine','active-replay','cancel','accepted-refresh','rejected','legacy','legacy-rejected','conflict','lost-live'].includes(answerFault)){
  const posts=()=>traffic.filter(t=>t.path==='/assist').length;
  const poll=async()=>{for(const fn of [...timers.values()])await act(async()=>{await fn();await ticks()});await flush()};
  const privateUI=()=>assert.doesNotMatch(document.documentElement.outerHTML,/synthetic-sensitive|Traceback|synthetic-stack/);
  if(answerFault==='status-query'){
   queryFault=true;refreshFault=true;await render();privateUI();assert.match(document.body.textContent,/Backend unavailable/);await click('Retry');privateUI();return;
  }
  if(answerFault==='lost-live'){
   await rpc({control:'segment'});await refresh();
   answerFault='';await click('Suggest answer');await poll();await refresh();
   answerFault='lost-live';await rpc({control:'hold-answer'});
   await click('Ask again');privateUI();assert.match(document.body.textContent,/not confirmed/);
   await click('Recheck status');assert.equal(posts(),2);
   await refresh();await poll();assert.match(document.body.textContent,/Synthetic live partial/);
   await rpc({control:'release-answer'});await poll();await refresh();
   assert.match(document.body.textContent,/Synthetic live partial completed/);assert.equal(posts(),2);return;
  }
  if(answerFault==='conflict'){
   await rpc({control:'hold-answer'});
   const active=await rpc({path:'/assist',method:'POST',body:{question:'Other synthetic client'}});
   assert.equal(active.code,200);
   // Deliberately stale UI: the real router must reject this submission.
   await click('Suggest answer');privateUI();assert.match(document.body.textContent,/not accepted because another answer is active/);
   assert.doesNotMatch(document.body.textContent,/may already be running/);
   await refresh();await poll();assert.match(document.body.textContent,/Synthetic live partial/);
   assert.equal(posts(),1);await rpc({control:'release-answer'});return;
  }
  if(['rejected','legacy-rejected'].includes(answerFault)){
   await refresh();await click('Suggest answer');privateUI();
   if(answerFault==='legacy-rejected'){
    assert.match(document.body.textContent,/not confirmed.*may already be running/);
    assert.ok(button('Recheck status'));await click('Recheck status');
   }else{
    assert.match(document.body.textContent,/not accepted.*question|Enter a question/i);
    assert.doesNotMatch(document.body.textContent,/may already be running/);
   }
   assert.equal(posts(),1);return;
  }
  await rpc({control:'segment'});await refresh();
  if(answerFault==='engine'){
   await rpc({control:'engine-failure'});await click('Suggest answer');await poll();await refresh();privateUI();
   const statusJSON=await rest('/status');assert.doesNotMatch(JSON.stringify(statusJSON),/synthetic-sensitive|Traceback/);
   assert.match(document.body.textContent,/Check answer setup/);assert.ok(button('Retry'));
   await rpc({control:'engine-failure',value:false});await click('Retry');await poll();await refresh();
   assert.equal(posts(),2);assert.match(document.body.textContent,/Synthetic offline answer/);return;
  }
  if(answerFault==='legacy'){
   await click('Suggest answer');await poll();await refresh();
   assert.match(document.body.textContent,/Synthetic offline answer/);assert.equal(posts(),1);return;
  }
  await click('Suggest answer');await poll();await refresh();
  await rpc({control:'hold-answer'});
  refreshFault=answerFault==='accepted-refresh';
  await click('Suggest answer');await poll();await refresh();
  assert.match(document.body.textContent,/Synthetic live partial/);
  if(answerFault==='active-replay'){
   assert.ok(button('Ask again').disabled,'history retry disabled while live');
   await act(async()=>button('Ask again').click());
   assert.equal(posts(),2,'conflicting replay sends no POST');
  }else if(answerFault==='cancel'){
   await click('Cancel answer');privateUI();
   assert.match(document.body.textContent,/Cancellation was not confirmed/);
  }else{
   assert.doesNotMatch(document.body.textContent,/submission was not confirmed/);privateUI();
  }
  await poll();assert.match(document.body.textContent,/Synthetic live partial/);
  await rpc({control:'release-answer'});await poll();await refresh();
  assert.match(document.body.textContent,/Synthetic live partial completed/);
  assert.equal(posts(),2);privateUI();console.log('PASS real answer '+answerFault);return;
 }
 if(process.env.ANSWER_FAULT){
  await rpc({control:'segment'});await refresh();
  if(answerFault==='provider')await rpc({control:'provider-failure'});
  if(answerFault==='replay'){
   answerFault='';await click('Suggest answer');await until(()=>traffic.some(t=>t.path.startsWith('/assist/')));await refresh();
   answerFault='replay';await click('Ask again');
  }else{
   pollFault=answerFault.startsWith('poll');
   await click('Suggest answer');
  }
  const initialCount=answerFault==='replay'?2:1;
  assert.equal(traffic.filter(t=>t.path==='/assist').length,initialCount);
  if(['lost','replay'].includes(answerFault)){
   assert.doesNotMatch(document.body.textContent,/synthetic-sensitive/);
   assert.match(document.body.textContent,/not confirmed/i);
   assert.ok(button('Recheck status'));
   await click('Recheck status');
   assert.equal(traffic.filter(t=>t.path==='/assist').length,initialCount,'status recheck must not replay');
   await refresh();assert.match(document.body.textContent,/Synthetic offline answer/);
  }else if(answerFault==='legacy-missing'){
   await until(()=>traffic.some(t=>t.path.startsWith('/assist/')));
   for(let i=0;i<5;i++)await act(async()=>{for(const fn of [...timers.values()])await fn();await ticks()});
   const polls=traffic.filter(t=>t.path.startsWith('/assist/'));
   assert.equal(polls.length,3);assert.ok(polls.every(t=>t.code===404));
   assert.match(document.body.textContent,/Polling is paused.*may still be running or may have been cleared/);
   assert.doesNotMatch(document.body.textContent,/no longer available/);
   assert.ok(!button('Retry'));
   await click('Recheck status');assert.equal(traffic.filter(t=>t.path==='/assist').length,1);
  }else if(answerFault==='missing'){
   await until(()=>traffic.some(t=>t.path.startsWith('/assist/')));
   for(let i=0;i<5;i++)await act(async()=>{for(const fn of [...timers.values()])await fn();await ticks()});
   assert.match(document.body.textContent,/no longer available/);
   const polls=traffic.filter(t=>t.path.startsWith('/assist/')).length;
   for(let i=0;i<3;i++)await act(async()=>{for(const fn of [...timers.values()])await fn();await ticks()});
   assert.equal(traffic.filter(t=>t.path.startsWith('/assist/')).length,polls,'missing answer stops polling');
   assert.ok(!button('Cancel answer'));
  }else if(answerFault.startsWith('poll')){
   for(let i=0;i<3;i++)await act(async()=>{for(const fn of [...timers.values()])await fn();await ticks()});
   assert.doesNotMatch(document.body.textContent,/synthetic-sensitive|no longer available/);
   assert.match(document.body.textContent,/Answer progress unavailable/);
   assert.ok(!button('Retry'),'poll failure must not replay an answer');
   status={...status,error:'Old provider error',error_request:{question:'old question'}};await render();
   assert.match(document.body.textContent,/Answer progress unavailable/);
   assert.ok(button('Recheck status'));assert.ok(!button('Retry'),'stale provider request must not own progress retry');
   await click('Recheck status');
   assert.equal(traffic.filter(t=>t.path==='/assist').length,initialCount);
   pollFault=false;
   for(const fn of [...timers.values()])await act(async()=>{await fn();await ticks()});
   await refresh();
   assert.match(document.body.textContent,/Synthetic offline answer/);
   assert.doesNotMatch(document.body.textContent,/Answer progress unavailable/);
  }else if(answerFault==='provider'){
   await until(()=>traffic.some(t=>t.path.startsWith('/assist/')));await refresh();
   assert.match(document.body.textContent,/Provider authentication failed \(HTTP 401\).*Check your API key/);
   assert.ok(button('Retry'),'confirmed provider failure retains its retry');
  }else{
   await until(()=>traffic.some(t=>t.path.startsWith('/assist/')));await refresh();
   assert.match(document.body.textContent,/Synthetic offline answer/);
  }
  console.log('PASS real answer '+answerFault);return;
 }
 if(process.env.CLAIM_CONFLICT){
  await act(async()=>document.querySelector('[aria-label="Capture permission"]').click());
  await click('Start listening');
  assert.match(document.body.textContent,/Another desktop renderer owns this backend/,'preserve actionable protocol diagnostics');
  assert.match(document.body.textContent,/watchdog initiates stop/);
  assert.equal(traffic.filter(t=>t.path==='/start').length,0);
  console.log('PASS real effects valid lifecycle rejection preserved');return;
 }
 if(failLifecycle){
  assert.doesNotMatch(document.body.textContent,/synthetic-sensitive/);
  assert.match(document.body.textContent,/reopen this pane/);
  const action=process.env.LIFECYCLE_ACTION||'start';
  if(action==='start')await act(async()=>document.querySelector('[aria-label="Capture permission"]').click());
  else { await rpc({control:'lifecycle-fixture',listening:action==='pause'});await refresh(); }
  await click({start:'Start listening',pause:'Pause listening',clear:'Clear meeting'}[action]);
  assert.doesNotMatch(document.body.textContent,/synthetic-sensitive/,'action retry must not reflect cached transport diagnostics');
  assert.match(document.body.textContent,/reopen.*pane|Retry.*action/i);
  assert.equal(traffic.filter(t=>['/start','/pause','/clear'].includes(t.path)).length,0,'no unauthorised action');
  console.log('PASS real effects lifecycle retry sanitization: '+action);return;
 }
 reports.push({initialKeyUnavailable:document.body.textContent.includes('Key settings unavailable'),strict:!!process.env.STRICT});
 if(document.body.textContent.includes('Key settings unavailable'))await click('Recheck key status');
 assert.equal(document.querySelector('[aria-label="Transcription provider"]').value,'local');
 assert.equal(button('Start listening').disabled,true);
 await key('fake-review-key-one');await click('Save key');
 assert.equal((await probe()).saved_v1,true);
 await click('Show overlay');await refresh();assert.equal(status.overlay_shown,true);
 if(process.env.ACTION_DIAGNOSTIC){
  const action=process.env.ACTION_DIAGNOSTIC;
  await act(async()=>document.querySelector('[aria-label="Capture permission"]').click());
  if(action==='pause'){await click('Start listening');await refresh();assert.equal(status.listening,true);}
  if(action==='clear'){await rpc({control:'segment'});await refresh();}
  if(!process.env.ACTION_TRANSPORT_FAULT)await rpc({control:'adapter-fault',action});
  await click({start:'Start listening',pause:'Pause listening',clear:'Clear meeting'}[action]);
  assert.ok(traffic.some(t=>t.path==='/'+action),'authorized action must reach the real router');
  assert.doesNotMatch(document.body.textContent,/synthetic-sensitive/,'action catch must never reflect raw diagnostics');
  await refresh();
  const raw=await rpc({path:'/status'});
  assert.doesNotMatch(JSON.stringify(raw)+document.body.textContent+stderr,/synthetic-sensitive/,'response and status must be safe too');
  if(action==='start')assert.match(document.body.textContent,/Check.*(audio|backend)|Retry.*Start/i);
  if(action==='pause'){
   assert.match(document.body.textContent,/Retry.*(Pause|stop)|verify.*(capture|indicator)/i);
   if(!process.env.ACTION_TRANSPORT_FAULT){assert.equal(raw.body.stop_confirmed,false);assert.equal((await probe()).running,true);assert.ok(button('Retry stop'));}
  }
  if(action==='clear')assert.match(document.body.textContent,/Clear.*(again|confirmed)|Retry.*Clear/i);
  console.log('PASS authorized '+action+' '+(process.env.ACTION_TRANSPORT_FAULT?'transport':'adapter')+' diagnostics');return;
 }
 if(process.env.DELAYED_FAILURE){
  await act(async()=>document.querySelector('[aria-label="Capture permission"]').click());
  await click('Start listening');await refresh();assert.equal(status.listening,true);
  await rpc({control:process.env.DELAYED_FAILURE==='409'?'conflict-shutdown-fault':'write-shutdown-fault'});
  holdWrite=true;
  await key('fake-review-key-two');await clickPending('Save key');await until(()=>heldWrite);
  assert.equal(heldWrite.code,Number(process.env.DELAYED_FAILURE));
  assert.equal(heldWrite.body.overlay_credentials,'retired');
  assert.equal(heldWrite.body.overlay_stop_confirmed,false);
  assert.match(heldWrite.body.detail,/overlay_shutdown_unconfirmed/);
  if(heldWrite.code===409)assert.match(heldWrite.body.detail,/changed|Recheck/i,'original conflict remains actionable');
  assert.doesNotMatch(JSON.stringify(heldWrite),/synthetic-sensitive/);
  failStatus=true;failReadback=true;
  await clickPending('Pause listening');
  await until(()=>traffic.some(t=>t.path==='/pause'&&t.code===200)&&!button('Pause listening')?.disabled);
  assert.equal((await probe()).running,false,'legitimate Pause completed, without replacing overlay');
  let newer;
  if(process.env.REPLACE_OVERLAY){
   await rpc({control:'cleanup-recover'});
   await clickPending('Hide overlay');await until(()=>button('Show overlay')&&!button('Show overlay').disabled);
   await clickPending('Show overlay');await until(()=>button('Hide overlay')&&!button('Hide overlay').disabled);
   newer=await probe();assert.ok(newer.overlay_owner);
  }
  releaseWrite();await flush();
  if(newer){
   assert.ok(button('Hide overlay'),'delayed error must not retire the new owner');
   assert.equal(button('Retry hide overlay'),undefined);
   assert.doesNotMatch(document.body.textContent,/shutdown is not confirmed|overlay was closed/i);
   assert.equal((await probe()).overlay_owner,newer.overlay_owner);
   await click('Hide overlay');
  }else{
   assert.ok(button('Retry hide overlay'),'uncertainty must survive intervening Pause and failed status');
   assert.equal(button('Show overlay'),undefined);
   assert.ok([...document.querySelectorAll('[role="alert"]')].some(e=>visible(e)&&/shutdown is not confirmed/.test(e.textContent)));
   await rpc({control:'cleanup-recover'});await click('Retry hide overlay');
   assert.ok(button('Show overlay'),'confirmed retry recovers');
   assert.equal(button('Retry hide overlay'),undefined);
  }
  assert.doesNotMatch(document.body.textContent+stderr,/synthetic-sensitive|fake-review-key/);
  console.log('PASS delayed '+process.env.DELAYED_FAILURE+' after Pause: '+(newer?'new owner fenced':'warning retained'));return;
 }
 if(process.env.DELAYED_STATUS){
  await rpc({control:'refresh-fault'});failReadback=true;holdStatus=true;
  await key('fake-review-key-two');await clickPending('Save key');await until(()=>heldStatus);
  assert.equal(heldStatus.body.overlay_shown,false);
  assert.ok(button('Show overlay'),'initial retirement was applied');
  holdStatus=false;
  await clickPending('Show overlay');await until(()=>button('Hide overlay')&&!button('Hide overlay').disabled);
  const newer=await probe();assert.ok(newer.overlay_owner);
  releaseStatus();await flush();
  assert.ok(button('Hide overlay'),'stale status fallback must not clear new owner');
  assert.doesNotMatch(document.body.textContent,/overlay was closed/i);
  assert.equal((await probe()).overlay_owner,newer.overlay_owner);
  assert.equal((await rest('/status')).overlay_shown,true);
  console.log('PASS real effects delayed status fallback preserves newer owner');return;
 }
 if(process.env.DELAYED_RETIREMENT){
  await rpc({control:'refresh-fault'});
  holdWrite=true;
  await key('fake-review-key-two');await clickPending('Save key');await until(()=>heldWrite);
  assert.equal(heldWrite.body.overlay_credentials,'retired');
  assert.equal((await rest('/status')).overlay_shown,false,'old overlay actually retired');
  await clickPending('Hide overlay');await until(()=>button('Show overlay')&&!button('Show overlay').disabled);
  await clickPending('Show overlay');await until(()=>button('Hide overlay')&&!button('Hide overlay').disabled);
  const newer=await probe();assert.ok(newer.overlay_owner);assert.equal(newer.overlay_v2,true);
  if(process.env.READBACK_FAULT)failReadback=true;
  releaseWrite();await flush();
  assert.ok(button('Hide overlay'),'stale retirement must not discard newer owner');
  assert.doesNotMatch(document.body.textContent,/overlay was closed|overlay is no longer shown/i);
  assert.equal((await probe()).overlay_owner,newer.overlay_owner,'response must not mutate backend ownership');
  assert.equal((await rest('/status')).overlay_shown,true);
  await click('Hide overlay');assert.equal((await rest('/status')).overlay_shown,false,'current owner remains usable');
  console.log('PASS real effects delayed retirement preserves newer owner');return;
 }
 await act(async()=>document.querySelector('[aria-label="Capture permission"]').click());
 await click('Start listening');await refresh();assert.equal(status.listening,true);
 await key('fake-review-key-two');await click('Save key');let p=await probe();
 assert.equal(p.overlay_v2,true);assert.equal(p.capture_v1,true);assert.equal(p.scoped_v2,true);assert.equal(p.private,true);assert.equal(p.process_unchanged,true);
 assert.equal(document.querySelector('[aria-label="Meta API key"]').value,'');
 assert.ok(visible(document.querySelector('[aria-label="Meta API key settings"]')));
 assert.ok(visible(button('Pause listening')));assert.ok(visible(button('Hide overlay')));
 assert.match(document.body.textContent,/fresh credential snapshot/);
 reports.push({liveEdit:'PASS',oldCaptureSnapshot:p.capture_v1,freshOverlaySnapshot:p.overlay_v2,controlsDisclosed:true});
 await rpc({control:'segment'});await refresh();await click('Pause listening');await refresh();await click('Resume listening');await refresh();assert.equal((await probe()).capture_v2,true);
 await rpc({control:process.env.CONFLICT_SHUTDOWN_FAULT?'conflict-shutdown-fault':process.env.WRITE_SHUTDOWN_FAULT?'write-shutdown-fault':'refresh-fault'});
 if(process.env.CONFLICT_SHUTDOWN_FAULT){failStatus=true;failReadback=true;}
 if(process.env.READBACK_FAULT)failReadback=true;
 await key('fake-review-key-one');await click('Save key');
 if(process.env.CONFLICT_SHUTDOWN_FAULT)assert.ok(traffic.some(t=>t.path==='/credentials/meta'&&t.code===409),'actual HTTP conflict');
 if(process.env.WRITE_SHUTDOWN_FAULT||process.env.CONFLICT_SHUTDOWN_FAULT){
  assert.ok(button('Retry hide overlay'));
  assert.equal(button('Show overlay'),undefined);
  assert.ok([...document.querySelectorAll('[role="alert"]')].some(e=>visible(e)&&/shutdown is not confirmed/.test(e.textContent)), 'uncertain shutdown must be a visible alert even without readback');
  assert.match(document.body.textContent,/could not be confirmed/);
  assert.doesNotMatch(document.body.textContent,/synthetic-sensitive/);
  assert.doesNotMatch(stderr,/synthetic-sensitive|fake-review-key/);
  console.log('PASS real effects uncertain write retains shutdown warning');return;
 }
 assert.ok(button('Show overlay'),'confirmed retirement immediately changes quick action before polling');
 if(process.env.READBACK_FAULT){assert.match(document.body.textContent,/could not be confirmed/);assert.match(document.body.textContent,/overlay was closed/);console.log('PASS real effects failed readback preserves retirement');return;}
 await refresh();
 assert.equal(status.overlay_shown,false);assert.equal(status.listening,true);
 const notice=[...document.querySelectorAll('[role="status"]')].find(e=>e.textContent.includes('credential refresh failed'));
 assert.ok(notice&&visible(notice));
 reports.push({rebindFault:'PASS',notice:notice.textContent,showButtonImmediately:!!button('Show overlay'),hideButtonImmediately:!!button('Hide overlay')});
 if(button('Hide overlay'))await click('Hide overlay');await click('Show overlay');await refresh();
 await click('Pause listening');await refresh();
 const provider=document.querySelector('[aria-label="Transcription provider"]');
 assert.equal(provider.disabled,false,'provider change must be a possible user interaction');
 assert.equal([...provider.options].find(o=>o.value==='meta').disabled,false);
 await act(async()=>{provider.value='meta';provider.dispatchEvent(new window.Event('change',{bubbles:true}));});
 await click('Remove saved key');await refresh();p=await probe();assert.equal(p.saved_v1,false);assert.equal(p.overlay_v1,false);assert.equal(p.overlay_v2,false);assert.equal(p.capture_v2,true);
 assert.equal(JSON.parse(store.get('meeting-copilot.scope.v2')).transcriptionProvider,'meta');
 assert.equal(JSON.parse(store.get('meeting-copilot.scope.v2')).transcriptionChoiceVersion,1);
 const audio=document.querySelector('[aria-label="Audio source"]');await act(async()=>{audio.value='system';audio.dispatchEvent(new window.Event('change',{bubbles:true}));});
 assert.equal(JSON.parse(store.get('meeting-copilot.scope.v2')).transcriptionProvider,'meta');
 assert.ok(visible(document.querySelector('[data-cloud-capture-disclosure]')));
 assert.equal(button('Resume listening').disabled,true,'unavailable explicitly selected cloud cannot start');
 assert.match(document.body.textContent,/Meta cloud transcription is unavailable/);
 reports.push({persistence:'PASS',cloudDisclosure:'PASS',savedConsent:JSON.parse(store.get('meeting-copilot.scope.v2')).consent??'not persisted'});
 assert.doesNotMatch(document.body.textContent,/fake-review-key|synthetic-sensitive-fault/);
 console.log(JSON.stringify({result:'PASS',react:React.version,reports,traffic},null,2));
})().catch(e=>{console.error(e.stack);process.exitCode=1}).finally(async()=>{await act(async()=>root.unmount());unregister();await ticks();child.stdin.end();assert.equal(await exit,0);if(stderr){console.error(stderr);process.exitCode=1;}dom.window.close();fs.rmSync(home,{recursive:true,force:true});});
