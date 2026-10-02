import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { JSDOM } from 'jsdom';

const ui = fileURLToPath(new URL('../../scripts/ssh_server_manager/assets/ui/', import.meta.url));
const html = await readFile(path.join(ui, 'index.html'), 'utf8');
const app = await readFile(path.join(ui, 'app.js'), 'utf8');
const servers = [
  {id:'api', alias:'api-prod',hostname:'api.example.invalid',username:'deploy',port:22,tags:['production'],proxy_jumps:['bastion'],notes:'Primary API\nKeep <script>literal</script>',credential_label:'SSH agent'},
  {id:'db',alias:'db-staging',hostname:'db.example.invalid',username:'postgres',port:2222,tags:['staging'],proxy_jumps:[],notes:'Staging database'},
  {id:'edge',alias:'edge-lab',hostname:'192.0.2.24',username:'lab',port:2200,tags:[],proxy_jumps:[],notes:''},
];
const bootstrap = {csrf:'fixture-csrf', servers,credentials:[],contexts:[{name:'production',count:1},{name:'staging',count:1},{name:'empty-tag',count:0}],skills:[],auth:{passkeys:0,master_password_enrolled:false,webauthn_available:false},managed_config:'/fixture/managed.conf'};
const sleep = () => new Promise(resolve => setTimeout(resolve, 10));
async function until(predicate) { for(let i=0;i<100;i++){if(predicate()) return; await sleep();} throw new Error('UI condition timed out'); }
async function start(t, {data=bootstrap, failures=0, hash=''}={}) {
  const dom = new JSDOM(html, { url:`http://localhost:8765/${hash}`, runScripts:'outside-only', pretendToBeVisual:true });
  t.after(()=>dom.window.close());
  const w=dom.window, doc=w.document, ctx=dom.getInternalVMContext();
  const requests=[], copied=[], errors=[], intervals=[];
  w.setInterval=(callback)=>{intervals.push(callback);return intervals.length;};
  w.matchMedia=()=>({matches:false,addEventListener(){}});
  w.confirm=()=>false;
  w.HTMLDialogElement.prototype.showModal=function(){ this.open=true; this.querySelector('button')?.focus(); };
  w.HTMLDialogElement.prototype.close=function(){ this.open=false; this.dispatchEvent(new w.Event('close')); };
  w.HTMLElement.prototype.getClientRects=function(){return this.hidden?[]:[{width:100,height:20}];};
  Object.defineProperty(w.navigator,'clipboard',{value:{writeText:async text=>copied.push(text)}});
  w.fetch=async (url, options={})=>{
    requests.push({url:String(url),method:options.method||'GET'});
    const failing=String(url)==='/api/bootstrap'&&failures-->0;
    return {ok:!failing,status:failing?503:200,headers:{get:()=> 'application/json'},json:async()=>failing?{message:'Fixture unavailable'}:structuredClone(data)};
  };
  w.addEventListener('error',e=>errors.push(e.message));
  const modules=new Map();
  const importer=async specifier=>{
    if(!specifier.startsWith('/assets/')||!/^\/assets\/[a-z]+\.js$/.test(specifier)) throw new Error('Unexpected module');
    if(!modules.has(specifier)){
      const mod=new vm.SourceTextModule(await readFile(path.join(ui,path.basename(specifier)),'utf8'), {context:ctx,identifier:specifier,importModuleDynamically:importer});
      modules.set(specifier,mod); await mod.link(importer); await mod.evaluate();
    }
    return modules.get(specifier);
  };
  // Run only after jsdom's own DOMContentLoaded; dispatch the app's event once.
  await sleep();
  new vm.Script(app,{filename:'app.js',importModuleDynamically:importer}).runInContext(ctx);
  doc.dispatchEvent(new w.Event('DOMContentLoaded'));
  await until(()=>requests.length>0);
  if(failures<0) await until(()=>doc.querySelector('link[href="/assets/hosts.css"]'));
  const input=(selector,value)=>{const el=doc.querySelector(selector);el.value=value;el.dispatchEvent(new w.Event('input',{bubbles:true}));};
  const click=selector=>{assert.ok(doc.querySelector(selector),`Missing ${selector}`);doc.querySelector(selector).click();};
  const visible=()=>[...doc.querySelectorAll('#serverRows tr')].filter(row=>!row.hidden);
  return {w,doc,requests,copied,errors,intervals,input,click,visible};
}

test('hosts-first inventory is searchable, tag-filtered, and never connects automatically',async t=>{
  const x=await start(t); await until(()=>x.doc.querySelector('link[href="/assets/hosts.css"]'));
  assert.equal(x.doc.body.dataset.view,'servers');
  assert.equal(x.doc.querySelector('#viewTitle').textContent,'Hosts');
  assert.equal(x.doc.querySelector('#hostNavigation').hidden,true);
  assert.equal(x.visible().length,3);
  assert.equal(x.doc.querySelectorAll('.connection-summary').length,0);
  x.input('[data-cx="search"]','POSTGRES'); assert.equal(x.visible().length,1);
  x.input('[data-cx="search"]','Primary API'); assert.equal(x.visible().length,1);
  x.input('[data-cx="search"]','missing-host'); assert.equal(x.visible().length,0);
  assert.equal(x.doc.querySelector('.cx-search-empty').hidden,false);
  x.input('[data-cx="search"]',''); assert.equal(x.visible().length,3);
  x.doc.querySelector('#hostContextSelect').value='tag:production';
  x.doc.querySelector('#hostContextSelect').dispatchEvent(new x.w.Event('change'));
  await until(()=>x.visible().length===1);
  assert.deepEqual(x.requests.map(r=>r.url),['/api/bootstrap']);
  assert.deepEqual(x.errors,[]);
});

test('details, copy, close, Back and Forward preserve the search and make no remote request',async t=>{
  const x=await start(t); await until(()=>x.doc.querySelector('link[href="/assets/hosts.css"]'));
  x.input('[data-cx="search"]','api'); x.click('[data-host-details="api"]');
  await until(()=>x.doc.querySelector('dialog[open]'));
  assert.equal(x.w.location.hash,'#/hosts/api-prod');
  assert.equal(x.doc.querySelector('dialog[open] [data-hd=command]').value,'serverctl connect api-prod');
  const dialog=x.doc.querySelector('dialog[open]');
  const copy=[...dialog.querySelectorAll('button')].find(b=>/Copy SSH command/.test(b.textContent)); copy.click();
  await until(()=>x.copied.length===1); assert.equal(x.copied[0],'serverctl connect api-prod');
  await until(()=>!copy.disabled); copy.click(); await until(()=>x.copied.length===2);
  x.w.history.back(); await until(()=>!x.doc.querySelector('dialog[open]'));
  x.w.history.forward(); await until(()=>x.doc.querySelector('dialog[open]'));
  const close=[...x.doc.querySelector('dialog[open]').querySelectorAll('button')].find(b=>/close/i.test(b.getAttribute('aria-label')||b.textContent)); close.click();
  await until(()=>!x.doc.querySelector('dialog[open]') && !x.w.location.hash); assert.equal(x.w.location.hash,'');
  assert.equal(x.doc.querySelector('[data-cx="search"]').value,'api'); assert.equal(x.visible().length,1);
  assert.deepEqual(x.requests.map(r=>r.url),['/api/bootstrap']); assert.deepEqual(x.errors,[]);
});

test('empty inventory offers add/import and empty tag offers reset',async t=>{
  const x=await start(t,{data:{...bootstrap,servers:[],contexts:[]}}); await until(()=>x.doc.querySelector('link[href="/assets/hosts.css"]'));
  assert.equal(x.doc.querySelector('#emptyServers').hidden,false);
  assert.equal(x.doc.querySelector('#emptyInventoryAddButton').hidden,false);
  assert.equal(x.doc.querySelector('#emptyInventoryImportButton').hidden,false);
  assert.equal(x.doc.querySelector('#emptyInventoryResetButton').hidden,true);
  x.click('#emptyInventoryAddButton'); assert.equal(x.doc.querySelector('#serverDialog').open,true);
  x.doc.querySelector('#serverDialog .close-dialog').click(); assert.equal(x.doc.querySelector('#serverDialog').open,false);
  assert.deepEqual(x.errors,[]);
});

test('bootstrap failure is distinct from empty state and retry recovers',async t=>{
  const x=await start(t,{failures:1}); await until(()=>!x.doc.querySelector('#hostsRetryButton').hidden);
  assert.match(x.doc.querySelector('#hostsLoadMessage').textContent,/Fixture unavailable/);
  assert.equal(x.doc.querySelector('#emptyServers').hidden,true);
  x.intervals.forEach(callback=>callback());
  assert.equal(x.doc.querySelector('#emptyServers').hidden,true, 'status tick must not turn failure into empty inventory');
  x.click('#hostsRetryButton'); await until(()=>x.doc.querySelector('link[href="/assets/hosts.css"]'));
  assert.equal(x.doc.querySelector('#hostsLoadState').hidden,true);
  assert.equal(x.visible().length,3); assert.equal(x.requests.length,2); assert.deepEqual(x.errors,[]);
});

test('accent and appearance controls remain independent of categorical tag colors',async t=>{
  const x=await start(t);
  x.doc.querySelector('#accentSelect').value='indigo'; x.doc.querySelector('#accentSelect').dispatchEvent(new x.w.Event('change'));
  assert.equal(x.doc.documentElement.dataset.accent,'indigo');
  assert.equal(x.w.localStorage.getItem('ssh-manager-accent'),'indigo');
  x.doc.querySelector('#themeSelect').value='dark'; x.doc.querySelector('#themeSelect').dispatchEvent(new x.w.Event('change'));
  assert.equal(x.doc.documentElement.dataset.theme,'dark');
  x.doc.querySelector('#accentSelect').value='teal'; x.doc.querySelector('#accentSelect').dispatchEvent(new x.w.Event('change'));
  assert.equal(x.doc.documentElement.dataset.accent,undefined); assert.deepEqual(x.errors,[]);
});
