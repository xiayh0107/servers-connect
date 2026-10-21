import {lazy,Suspense,useCallback,useEffect,useRef,useState} from 'react';
import {api,setCsrf} from './api';
import {useDialogState,useHash,usePreference} from './hooks';
import type {Credential,Host,RuntimeInfo} from './types';
import {Button,ErrorNotice,Icon,StatusDot} from './ui';
const CredentialEditor=lazy(()=>import('./pages/CredentialsPage').then(module=>({default:module.CredentialEditor})));
const HostsPage=lazy(()=>import('./pages/HostsPage'));
const FilesPage=lazy(()=>import('./pages/FilesPage'));
const TagsPage=lazy(()=>import('./pages/TagsPage'));
const PortsPage=lazy(()=>import('./pages/PortsPage'));
const AuditPage=lazy(()=>import('./pages/AuditPage'));
const SkillsPage=lazy(()=>import('./pages/SkillsPage'));
const CredentialsPage=lazy(()=>import('./pages/CredentialsPage'));

export default function App() {
  const hash=useHash(),[runtime,setRuntime]=useState<RuntimeInfo>(),[hosts,setHosts]=useState<Host[]>([]),[credentials,setCredentials]=useState<Credential[]>([]),[error,setError]=useState<unknown>(),[expired,setExpired]=useState(false),[loading,setLoading]=useState(true),[createCredential,setCreateCredential]=useDialogState<{done:(credential:Credential)=>void}|null>('host-new-credential',null);
  const [theme,setTheme]=usePreference('theme','system'),[accent,setAccent]=usePreference('accent','teal');
  const refreshSequence=useRef(0),initializeSequence=useRef(0);
  const refresh=useCallback(async()=>{const current=++refreshSequence.current;const [h,c]=await Promise.all([api<Host[]>('/hosts'),api<Credential[]>('/credentials')]);if(current===refreshSequence.current){setHosts(h);setCredentials(c);}},[]);
  const initialize=useCallback(async()=>{const current=++initializeSequence.current;setLoading(true);setError(undefined);try{const value=await api<RuntimeInfo>('/runtime');if(current!==initializeSequence.current)return;setCsrf(value.csrf);setRuntime(value);await refresh();if(current===initializeSequence.current)setExpired(false);}catch(reason){if(current===initializeSequence.current)setError(reason);}finally{if(current===initializeSequence.current)setLoading(false);}},[refresh]);
  useEffect(()=>{void initialize();const expire=()=>{initializeSequence.current++;setExpired(true);};window.addEventListener('ssm-session-expired',expire);return()=>{refreshSequence.current++;initializeSequence.current++;window.removeEventListener('ssm-session-expired',expire);};},[initialize]);
  useEffect(()=>{const media=matchMedia('(prefers-color-scheme: dark)');const apply=()=>{document.documentElement.dataset.theme=theme==='system'?(media.matches?'dark':'light'):theme;document.documentElement.dataset.accent=accent;};apply();media.addEventListener('change',apply);return()=>media.removeEventListener('change',apply);},[theme,accent]);
  const section=hash.startsWith('#/servers/')?'hosts':hash.split('?')[0].split('/')[1]||'hosts';
  if(expired) return <div className="session-screen"><h1>Browser session expired</h1><p>Open a new session with <code>serverctl ui</code>. Your previous action has not been replayed.</p></div>;
  if(!runtime||error) return <div className="session-screen"><h1>SSH Server Manager</h1>{loading?<p>Loading local workspace…</p>:<><ErrorNotice error={error}/><Button onClick={()=>void initialize()}>Retry initialization</Button></>}</div>;
  return <div className="app-shell"><aside className="sidebar"><div className="brand"><div className="brand-mark"><Icon name="terminal"/></div><div><strong>Server Manager</strong><small>Local · {location.hostname}</small></div></div><nav className="nav" aria-label="Primary navigation">{[['hosts','Hosts'],['ports','Ports'],['tags','Tags'],['skills','Skills'],['credentials','Credentials'],['audit','Audit']].map(([id,label])=><a key={id} aria-label={label} className={section===id?'active':''} href={`#/${id}`} aria-current={section===id?'page':undefined}><Icon name={id}/><span>{label}</span>{id==='hosts'&&<span className="count">{hosts.length}</span>}{id==='credentials'&&<span className="count">{credentials.length}</span>}</a>)}</nav><div className="recent"><span className="nav-label">Recent</span>{[...hosts].filter(h=>h.last_success_at).sort((a,b)=>(b.last_success_at||'').localeCompare(a.last_success_at||'')).slice(0,3).map(host=><a key={host.id} href={`#/hosts/${encodeURIComponent(host.alias)}`}><StatusDot tone={host.last_test_status || ''}/>{host.alias}</a>)}</div><footer className="sidebar-footer"><div className="session-heading"><StatusDot tone="ok"/>Browser session</div><SessionExpiry runtime={runtime}/><span>Secrets in {runtime.vault?.name || 'OS credential vault'}</span><div className="theme-controls"><select aria-label="Theme" value={theme} onChange={e=>setTheme(e.target.value)}><option value="system">System</option><option value="light">Light</option><option value="dark">Dark</option><option value="contrast">High contrast</option></select><select aria-label="Accent" value={accent} onChange={e=>setAccent(e.target.value)}>{['teal','indigo','emerald','amber','rose','violet','graphite'].map(a=><option key={a}>{a}</option>)}</select></div><small>v{runtime.version} · loopback only</small></footer></aside><main className="main" data-section={section}><Suspense fallback={<div className="page-loading">Loading page…</div>}>{page(runtime,hosts,credentials,hash,refresh,initialize,setCreateCredential)}</Suspense></main>{createCredential&&<Suspense fallback={<div className="page-loading">Loading credential form…</div>}><CredentialEditor runtime={runtime} onClose={()=>setCreateCredential(null)} onSaved={async credential=>{await refresh();createCredential.done(credential);setCreateCredential(null);}}/></Suspense>}</div>;
}

function page(runtime:RuntimeInfo,hosts:Host[],credentials:Credential[],hash:string,refresh:()=>Promise<void>,initialize:()=>Promise<void>,setCreateCredential:(value:{done:(credential:Credential)=>void})=>void){
  const section=hash.startsWith('#/servers/')?'hosts':hash.split('?')[0].split('/')[1]||'hosts';
  if(section==='ports') return <PortsPage hosts={hosts}/>;
  if(section==='audit') return <AuditPage/>;
  if(section==='files') return <FilesPage hosts={hosts} runtime={runtime} initialHostId={decodeHashPart(hash.split('/')[2]?.split('?')[0])} initialPath={new URLSearchParams(hash.split('?')[1]||'').get('path')||undefined}/>;
  if(section==='tags') return <TagsPage hosts={hosts} onRefresh={refresh}/>;
  if(section==='skills') return <SkillsPage hosts={hosts}/>;
  if(section==='credentials') return <CredentialsPage hosts={hosts} credentials={credentials} runtime={runtime} onRefresh={refresh} onRuntimeRefresh={initialize}/>;
  return <HostsPage hosts={hosts} credentials={credentials} runtime={runtime} onRefresh={refresh} hash={hash} onCreateCredential={done=>setCreateCredential({done})}/>;
}
function decodeHashPart(part?:string){try{return part?decodeURIComponent(part):undefined;}catch{return undefined;}}
function SessionExpiry({runtime}:{runtime:RuntimeInfo}) {
  const [now,setNow]=useState(Date.now()),[offset]=useState(()=>Date.parse(runtime.server_time)-Date.now());useEffect(()=>{const timer=setInterval(()=>setNow(Date.now()),60000);return()=>clearInterval(timer);},[]);
  const session=runtime.session;
  const expires=session?Date.parse(session.expires_at):NaN;
  const remaining=Math.max(0,Math.ceil((expires-now-offset)/60000));
  return <span>Loopback only · {Number.isFinite(expires)?remaining>=60?`expires in ${Math.floor(remaining/60)} h${remaining%60?` ${remaining%60} m`:''}`:`expires in ${remaining} m`:'expiry unavailable'}</span>;
}
