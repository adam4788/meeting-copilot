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
async function rest(path,options={}){if(failLifecycle && path===failLifecycle)throw new Error('synthetic-sensitive-lifecycle');if(failReadback&&path==='/credentials/meta'&&!options.method)throw new Error('synthetic-sensitive-readback');const r=await rpc({path,...options});traffic.push({path,method:options.method||'GET',code:r.code});if(r.code>=400)throw new Error('HTTP '+r.code+' '+JSON.stringify(r.body));return r.body;}
let failReadback=false, failLifecycle=process.env.INITIAL_LIFECYCLE_FAULT ? (process.env.INITIAL_LIFECYCLE_FAULT==='claim'?'/overlay/claim':'/overlay/lifecycle') : '';
let status={capabilities:{macos_transcription:true,overlay:true,meta_asr:true}},registered;
const ctx={rest,register:c=>{if(c.area==='panes')registered=c;return()=>{}}};
const source=fs.readFileSync(__dirname+'/desktop/plugin.js','utf8').replace(/^import .*;\r?\n/gm,'').replace('export default','const plugin =');
const store=new Map([['meeting-copilot.scope.v2',JSON.stringify({backend:'macos',transcriptionProvider:'meta'})]]);
const box={...React,h:React.createElement,ctx,crypto:require('node:crypto').webcrypto,console,window:dom.window,
Button:'button',Input:'input',Textarea:'textarea',Badge:'span',PALETTE_AREA:'palette',setInterval:()=>0,clearInterval:()=>{},setTimeout,clearTimeout,
host:{notify:()=>{},paneVisibility:()=>({get:()=>true}),revealPane:()=>{}},useValue:a=>a.get(),useQueryClient:()=>({invalidateQueries:async()=>{}}),
useQuery:q=>({data:q.queryKey[1]==='status'?status:q.queryKey[1]==='models'?{providers:[]}:[]}),
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
async function key(value){const field=document.querySelector('[aria-label="Meta API key"]');await act(async()=>{Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set.call(field,value);field.dispatchEvent(new window.Event('input',{bubbles:true}));});}
const probe=()=>rest('/review/probe');
const visible=e=>{for(let n=e;n;n=n.parentElement)if(n.tagName==='DETAILS'&&!n.open)return false;return true;};
const reports=[];
(async()=>{
 await render();
 if(failLifecycle){assert.doesNotMatch(document.body.textContent,/synthetic-sensitive/);assert.match(document.body.textContent,/reopen this pane/);console.log('PASS real effects initial lifecycle sanitization');return;}
 reports.push({initialKeyUnavailable:document.body.textContent.includes('Key settings unavailable'),strict:!!process.env.STRICT});
 if(document.body.textContent.includes('Key settings unavailable'))await click('Recheck key status');
 assert.equal(document.querySelector('[aria-label="Transcription provider"]').value,'local');
 assert.equal(button('Start listening').disabled,true);
 await key('fake-review-key-one');await click('Save key');
 assert.equal((await probe()).saved_v1,true);
 await click('Show overlay');await refresh();assert.equal(status.overlay_shown,true);
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
 await rpc({control:process.env.WRITE_SHUTDOWN_FAULT?'write-shutdown-fault':'refresh-fault'});
 if(process.env.READBACK_FAULT)failReadback=true;
 await key('fake-review-key-one');await click('Save key');
 if(process.env.WRITE_SHUTDOWN_FAULT){assert.ok(button('Retry hide overlay'));assert.match(document.body.textContent,/shutdown is not confirmed/);assert.match(document.body.textContent,/could not be confirmed/);assert.doesNotMatch(document.body.textContent,/synthetic-sensitive/);console.log('PASS real effects uncertain write retains shutdown warning');return;}
 assert.ok(button('Show overlay'),'confirmed retirement immediately changes quick action before polling');
 if(process.env.READBACK_FAULT){assert.match(document.body.textContent,/could not be confirmed/);assert.match(document.body.textContent,/overlay was closed/);console.log('PASS real effects failed readback preserves retirement');return;}
 await refresh();
 assert.equal(status.overlay_shown,false);assert.equal(status.listening,true);
 const notice=[...document.querySelectorAll('[role="status"]')].find(e=>e.textContent.includes('credential refresh failed'));
 assert.ok(notice&&visible(notice));
 reports.push({rebindFault:'PASS',notice:notice.textContent,showButtonImmediately:!!button('Show overlay'),hideButtonImmediately:!!button('Hide overlay')});
 if(button('Hide overlay'))await click('Hide overlay');await click('Show overlay');await refresh();
 const provider=document.querySelector('[aria-label="Transcription provider"]');
 assert.equal([...provider.options].find(o=>o.value==='meta').disabled,false);
 await act(async()=>{provider.value='meta';provider.dispatchEvent(new window.Event('change',{bubbles:true}));});
 await click('Remove saved key');p=await probe();assert.equal(p.saved_v1,false);assert.equal(p.overlay_v1,false);assert.equal(p.overlay_v2,false);assert.equal(p.capture_v2,true);
 await click('Pause listening');await refresh();
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
