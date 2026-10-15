import {useCallback, useEffect, useRef, useState, type SetStateAction} from 'react';
import {api} from './api';

export function useResource<T>(path: string | null) {
  const [data, setData] = useState<T | undefined>(), [error, setError] = useState<unknown>(), [loading, setLoading] = useState(false), [revision, setRevision] = useState(0);
  const sequence = useRef(0);
  useEffect(() => {
    const request = ++sequence.current, controller = new AbortController();
    setData(undefined); setError(undefined);
    if (!path) {setLoading(false); return () => controller.abort();}
    setLoading(true);
    api<T>(path, {signal: controller.signal}).then(value => {if (request === sequence.current) setData(value);}).catch(reason => {if (!controller.signal.aborted && request === sequence.current) setError(reason);}).finally(() => {if (!controller.signal.aborted && request === sequence.current) setLoading(false);});
    return () => {controller.abort(); sequence.current++;};
  }, [path, revision]);
  return {data, error, loading, reload: useCallback(() => setRevision(value => value + 1), [])};
}
export function useHash() {
  const [hash, setHash] = useState(location.hash || '#/hosts');
  useEffect(() => {const update = () => setHash(location.hash || '#/hosts'); window.addEventListener('hashchange', update); return () => window.removeEventListener('hashchange', update);}, []);
  return hash;
}
export function usePreference<T>(name: string, fallback: T) {
  const [value, update] = useState<T>(() => {try {const stored = localStorage.getItem(`ssm.${name}`); if(stored)return JSON.parse(stored) as T;const legacy=name==='theme'?localStorage.getItem('ssh-manager-theme'):name==='accent'?localStorage.getItem('ssh-manager-accent'):null;return legacy?legacy as T:fallback;} catch {return fallback;}});
  const set = (next: T) => {update(next); try {localStorage.setItem(`ssm.${name}`, JSON.stringify(next));} catch { /* storage is optional */ }};
  return [value, set] as const;
}

// Dialog payloads are ephemeral UI metadata, never input secrets. URL state
// records only names; backward/forward navigation never repeats an operation.
const dialogValues=new Map<string,unknown>();
let dialogBackPending=false;
const dialogNavigationQueue:(()=>void)[]=[];
window.addEventListener('popstate',()=>{if(!dialogBackPending)return;dialogBackPending=false;queueMicrotask(()=>{while(!dialogBackPending&&dialogNavigationQueue.length)dialogNavigationQueue.shift()!();});});
function dialogNavigate(action:()=>void){if(dialogBackPending)dialogNavigationQueue.push(action);else action();}
export function useDialogState<T>(name:string,initial:T) {
  const active=()=>new URLSearchParams(location.hash.split('?')[1]||'').getAll('dialog').includes(name);
  const [value,update]=useState<T>(()=>active()&&dialogValues.has(name)?dialogValues.get(name) as T:initial);
  const current=useRef(value);current.current=value;
  const alive=useRef(true);
  useEffect(()=>()=>{alive.current=false;},[]);
  useEffect(()=>{const sync=()=>update(active()&&dialogValues.has(name)?dialogValues.get(name) as T:initial);window.addEventListener('hashchange',sync);window.addEventListener('popstate',sync);return()=>{window.removeEventListener('hashchange',sync);window.removeEventListener('popstate',sync);};},[name,initial]);
  const set=useCallback((action:SetStateAction<T>)=>{const next=typeof action==='function'?(action as (old:T)=>T)(current.current):action;current.current=next;update(next);if(next)dialogValues.set(name,next);dialogNavigate(()=>{if(!alive.current)return;const base=location.hash.split('?')[0]||'#/hosts',params=new URLSearchParams(location.hash.split('?')[1]||'');const names=params.getAll('dialog');
    if(next){dialogValues.set(name,next);if(!names.includes(name)){params.append('dialog',name);history.pushState({...history.state,ssmDialog:name},'',`${base}?${params}`);window.dispatchEvent(new Event('hashchange'));}}
    else if(names.includes(name)){if(history.state?.ssmDialog===name){dialogBackPending=true;history.back();}else{params.delete('dialog');for(const item of names.slice(0,names.indexOf(name)))params.append('dialog',item);history.replaceState(history.state,'',`${base}${params.size?'?'+params:''}`);window.dispatchEvent(new Event('hashchange'));}}
  });},[name]);
  return [value,set] as const;
}
