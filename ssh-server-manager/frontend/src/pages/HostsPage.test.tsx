import {act,fireEvent,render,screen,waitFor,within} from '@testing-library/react';
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import userEvent from '@testing-library/user-event';
import {api} from '../api';
import {credential,host,runtime} from '../test-fixtures';
import HostsPage,{visibleHostText} from './HostsPage';
vi.mock('../api',async original=>({...await original<typeof import('../api')>(),api:vi.fn()}));
const mocked=vi.mocked(api);
beforeEach(()=>{mocked.mockReset();history.replaceState({},'','#/hosts');localStorage.clear();});
afterEach(()=>{vi.useRealTimers();});
it('opens local workspace and route without an implicit remote operation',async()=>{
  const value=host();mocked.mockImplementation(async path=>path.endsWith('/workspace')?{paths:[],skills:[]}:path.endsWith('/route')?{hops:[{alias:value.alias,external:false}],blocked_by:null}:Promise.reject(new Error('Unexpected remote action')));
  render(<HostsPage hosts={[value]} credentials={[]} runtime={runtime} hash="#/hosts/alpha" onRefresh={vi.fn()} onCreateCredential={vi.fn()}/>);
  await screen.findByRole('dialog',{name:'Host details alpha'});await waitFor(()=>expect(mocked.mock.calls.some(([path])=>path.endsWith('/route'))).toBe(true));
  expect(mocked.mock.calls.every(([path,options])=>!options?.method&&(/workspace$|route$/).test(path))).toBe(true);
});
it('does not render or search hidden endpoint, credential or note values',()=>{
  const value=host({hostname:'private-endpoint.invalid',notes:'private-note',credential_label:'private-credential'});
  expect(visibleHostText(value,false)).not.toContain('private');localStorage.setItem('ssm.host-details','false');
  render(<HostsPage hosts={[value]} credentials={[]} runtime={runtime} hash="#/hosts" onRefresh={vi.fn()} onCreateCredential={vi.fn()}/>);
  expect(screen.queryByText(/private-endpoint/)).toBeNull();expect(screen.queryByText('private-credential')).toBeNull();
  fireEvent.change(screen.getByRole('textbox',{name:/Search hosts/}),{target:{value:'private-note'}});expect(screen.queryByRole('button',{name:'alpha'})).toBeNull();
});
it('shows a cached hover preview without contacting the host',async()=>{
  vi.useFakeTimers();
  const value=host({facts:{os_logo:'ubuntu',os_pretty:'Ubuntu 24.04 LTS',arch:'x86_64',cpu_cores:'8',cpu_model:'AMD EPYC 7B13',memory:'Mem: 30.5Gi 11.2Gi 19.3Gi 0B 0B',disk:'/dev/nvme0n1p1 460G 120G 340G 27% /',load:'0.42 0.38 0.35',uptime:'up 14 days, 3 hours',observed_at:new Date().toISOString()}});
  render(<HostsPage hosts={[value]} credentials={[]} runtime={runtime} hash="#/hosts" onRefresh={vi.fn()} onCreateCredential={vi.fn()}/>);
  const row=screen.getByRole('row',{name:/alpha/}),name=screen.getByRole('button',{name:'alpha'});
  fireEvent.mouseEnter(row);
  await act(async()=>{vi.advanceTimersByTime(300);});
  expect(screen.queryByRole('tooltip')).toBeNull();
  fireEvent.mouseEnter(name);
  await act(async()=>{vi.advanceTimersByTime(300);});
  const preview=screen.getByRole('tooltip');
  expect(preview).toHaveTextContent('Runtime');
  expect(preview).toHaveTextContent('30.5 GiB');
  expect(preview).toHaveTextContent('8 vCPU');
  expect(preview).toHaveTextContent('37% used');
  fireEvent.mouseLeave(name);
  await act(async()=>{vi.advanceTimersByTime(200);});
  expect(screen.queryByRole('tooltip')).toBeNull();
  expect(mocked).not.toHaveBeenCalled();
  vi.useRealTimers();
});

it('collects on deliberate hover, loads missing values inline and reuses the result',async()=>{
  vi.useFakeTimers();
  let finish!:(value:unknown)=>void;
  const updated=host({facts:{os_logo:'ubuntu',os_pretty:'Ubuntu 24.04 LTS',arch:'x86_64',cpu_cores:'8',memory:'Mem: 30.5Gi 11.2Gi',observed_at:new Date().toISOString()}}),refresh=vi.fn().mockResolvedValue(undefined);
  mocked.mockImplementation(async(path,options)=>options?.method==='POST'?new Promise(done=>{finish=done;}):path==='/hosts/host-a'?updated:undefined);
  const view=render(<HostsPage hosts={[host()]} credentials={[]} runtime={runtime} hash="#/hosts" onRefresh={refresh} onCreateCredential={vi.fn()}/>);
  expect(screen.queryByRole('button',{name:/Collect host info/})).toBeNull();
  const name=screen.getByRole('button',{name:'alpha'});
  fireEvent.mouseEnter(name);
  await act(async()=>{vi.advanceTimersByTime(259);});
  expect(mocked).not.toHaveBeenCalled();
  await act(async()=>{vi.advanceTimersByTime(1);});
  expect(screen.getByLabelText('Loading OS')).toBeVisible();
  expect(screen.getByLabelText('Loading CPU load')).toBeVisible();
  expect(screen.getByRole('tooltip')).toHaveTextContent('Refreshing');
  expect(mocked).toHaveBeenCalledExactlyOnceWith('/hosts/host-a/observe',expect.objectContaining({method:'POST',body:JSON.stringify({timeout:8})}));
  fireEvent.mouseLeave(name);await act(async()=>{vi.advanceTimersByTime(150);});
  fireEvent.mouseEnter(name);await act(async()=>{vi.advanceTimersByTime(300);});
  expect(mocked).toHaveBeenCalledTimes(1);
  await act(async()=>finish({ok:true}));
  expect(within(screen.getByRole('tooltip')).getByText('Ubuntu 24.04 LTS')).toBeVisible();
  expect(screen.queryByLabelText('Loading OS')).toBeNull();
  expect(refresh).toHaveBeenCalledOnce();
  fireEvent.mouseLeave(name);await act(async()=>{vi.advanceTimersByTime(150);});
  fireEvent.mouseEnter(name);await act(async()=>{vi.advanceTimersByTime(300);});
  expect(mocked.mock.calls.filter(([,options])=>options?.method==='POST')).toHaveLength(1);
  view.rerender(<HostsPage hosts={[host({last_test_at:new Date().toISOString(),last_test_status:'failed'})]} credentials={[]} runtime={runtime} hash="#/hosts" onRefresh={refresh} onCreateCredential={vi.fn()}/>);
  expect(screen.getByRole('tooltip')).toHaveTextContent('Failed');
  expect(screen.getByRole('tooltip')).toHaveTextContent('Ubuntu 24.04 LTS');
});

it('does not collect for passing hovers or keyboard-focus restoration',async()=>{
  vi.useFakeTimers();
  render(<HostsPage hosts={[host()]} credentials={[]} runtime={runtime} hash="#/hosts" onRefresh={vi.fn()} onCreateCredential={vi.fn()}/>);
  const name=screen.getByRole('button',{name:'alpha'});
  fireEvent.mouseEnter(name);await act(async()=>{vi.advanceTimersByTime(200);});fireEvent.mouseLeave(name);
  await act(async()=>{vi.advanceTimersByTime(300);});fireEvent.focus(name);
  expect(screen.getByRole('tooltip')).toHaveTextContent('Not observed yet');
  expect(mocked).not.toHaveBeenCalled();
});

it('serializes hover observations and skips a host that is no longer hovered',async()=>{
  vi.useFakeTimers();
  let finish!:(value:unknown)=>void;
  const values=[host(),host({id:'host-b',alias:'beta'}),host({id:'host-c',alias:'gamma'})];
  mocked.mockImplementation(async(path,options)=>options?.method==='POST'?new Promise(done=>{finish=done;}):values.find(value=>path.endsWith(value.id)));
  render(<HostsPage hosts={values} credentials={[]} runtime={runtime} hash="#/hosts" onRefresh={vi.fn()} onCreateCredential={vi.fn()}/>);
  for(const alias of ['alpha','beta','gamma']){fireEvent.mouseEnter(screen.getByRole('button',{name:alias}));await act(async()=>{vi.advanceTimersByTime(300);});}
  expect(mocked.mock.calls.filter(([,options])=>options?.method==='POST')).toHaveLength(1);
  await act(async()=>finish({ok:true}));
  expect(mocked.mock.calls.filter(([,options])=>options?.method==='POST').map(([path])=>path)).toEqual(['/hosts/host-a/observe','/hosts/host-c/observe']);
  expect(screen.getByRole('tooltip')).toHaveTextContent('gamma');
  expect(screen.getByRole('tooltip')).not.toHaveTextContent('alpha@');
  await act(async()=>finish({ok:false}));
});

it('keeps saved facts on failure, stops loading and applies a retry cooldown',async()=>{
  vi.useFakeTimers();mocked.mockResolvedValue({ok:false,message:'Remote probe failed'});
  const value=host({facts:{os_logo:'unknown',os_pretty:'Saved Linux',cpu_cores:'8',observed_at:'2026-10-01T00:00:00Z'}});
  render(<HostsPage hosts={[value]} credentials={[]} runtime={runtime} hash="#/hosts" onRefresh={vi.fn()} onCreateCredential={vi.fn()}/>);
  const name=screen.getByRole('button',{name:'alpha'});
  fireEvent.mouseEnter(name);await act(async()=>{vi.advanceTimersByTime(300);});
  expect(screen.getByRole('tooltip')).toHaveTextContent('Saved Linux');
  expect(screen.getByRole('tooltip')).toHaveTextContent('Refresh unavailable');
  expect(screen.queryByLabelText('Loading OS')).toBeNull();
  fireEvent.mouseLeave(name);await act(async()=>{vi.advanceTimersByTime(150);});
  fireEvent.mouseEnter(name);await act(async()=>{vi.advanceTimersByTime(300);});expect(mocked).toHaveBeenCalledTimes(1);
  fireEvent.mouseLeave(name);await act(async()=>{vi.advanceTimersByTime(30_000);});
  fireEvent.mouseEnter(name);await act(async()=>{vi.advanceTimersByTime(300);});expect(mocked).toHaveBeenCalledTimes(2);
});

it('retains masked lanes and route order, disables copying, and hides all private values',()=>{
  localStorage.setItem('ssm.host-details','false');
  const child=host({id:'child',alias:'child',proxy_jumps:['jump'],hostname:'private.invalid',username:'private-user',credential_id:'secret',credential_label:'private-key',notes:'private-note',geo:{city:'private-city',country_code:'US',source:'manual'}});
  render(<HostsPage hosts={[child,host({alias:'jump',jump_for_count:1}),host({id:'other',alias:'other'})]} credentials={[]} runtime={runtime} hash="#/hosts" onRefresh={vi.fn()} onCreateCredential={vi.fn()}/>);
  expect(screen.getByRole('columnheader',{name:'Endpoint'})).toBeVisible();
  expect(screen.getByRole('columnheader',{name:'Credential'})).toBeVisible();
  expect(screen.getAllByLabelText('Endpoint hidden')).toHaveLength(3);
  expect(screen.getByLabelText('Credential hidden')).toBeVisible();
  expect(screen.getByRole('button',{name:'Copy SSH command child'})).toBeDisabled();
  expect(document.body.textContent).not.toMatch(/private\.invalid|private-user|private-key|private-note|private-city/);
  expect([...document.querySelectorAll('.host-row')].map(row=>row.getAttribute('data-host'))).toEqual(['jump','child','other']);
  expect(document.querySelector('.route-child')).toHaveClass('route-last');
  expect(document.querySelector('.group-row')).toBeNull();
});

it('revalidates an expired snapshot without blanking saved values or fabricating CPU load',async()=>{
  vi.useFakeTimers();
  let finish!:(value:unknown)=>void;
  const value=host({facts:{os_logo:'ubuntu',os_pretty:'Saved Linux',cpu_cores:'8',observed_at:new Date().toISOString()}});
  mocked.mockImplementation(async(_path,options)=>options?.method==='POST'?new Promise(done=>{finish=done;}):value);
  render(<HostsPage hosts={[value]} credentials={[]} runtime={runtime} hash="#/hosts" onRefresh={vi.fn()} onCreateCredential={vi.fn()}/>);
  const name=screen.getByRole('button',{name:'alpha'});
  fireEvent.mouseEnter(name);await act(async()=>{vi.advanceTimersByTime(300);});
  expect(screen.getByRole('tooltip')).not.toHaveTextContent('CPU load');
  expect(mocked).not.toHaveBeenCalled();
  fireEvent.mouseLeave(name);await act(async()=>{vi.advanceTimersByTime(61_000);});
  fireEvent.mouseEnter(name);await act(async()=>{vi.advanceTimersByTime(300);});
  expect(within(screen.getByRole('tooltip')).getByText('Saved Linux')).toBeVisible();
  expect(screen.queryByLabelText('Loading OS')).toBeNull();
  expect(screen.getByLabelText('Loading CPU load')).toBeVisible();
  await act(async()=>finish({ok:false}));
});

it('does not reveal a late observation after masking or reuse it for a changed endpoint',async()=>{
  vi.useFakeTimers();
  let finish!:(value:unknown)=>void;
  mocked.mockImplementation(async(_path,options)=>options?.method==='POST'?new Promise(done=>{finish=done;}):host({facts:{os_logo:'ubuntu',os_pretty:'Old endpoint Linux',observed_at:new Date().toISOString()}}));
  const props={hosts:[host()],credentials:[],runtime,hash:'#/hosts',onRefresh:vi.fn(),onCreateCredential:vi.fn()};
  const view=render(<HostsPage {...props}/>);
  fireEvent.mouseEnter(screen.getByRole('button',{name:'alpha'}));await act(async()=>{vi.advanceTimersByTime(300);});
  fireEvent.click(screen.getByRole('button',{name:'Hide details'}));
  await act(async()=>finish({ok:true}));
  expect(screen.queryByRole('tooltip')).toBeNull();
  expect(screen.queryByText('Old endpoint Linux')).toBeNull();
  view.rerender(<HostsPage {...props} hosts={[host({hostname:'changed.invalid'})]}/>);
  fireEvent.click(screen.getByRole('button',{name:'Show details'}));
  fireEvent.mouseEnter(screen.getByRole('button',{name:'alpha'}));await act(async()=>{vi.advanceTimersByTime(300);});
  expect(screen.queryByText('Old endpoint Linux')).toBeNull();
  expect(screen.getByLabelText('Loading OS')).toBeVisible();
  expect(mocked.mock.calls.filter(([,options])=>options?.method==='POST')).toHaveLength(2);
  await act(async()=>finish({ok:false}));
});

it('aborts an outstanding preview when the inventory unmounts',async()=>{
  vi.useFakeTimers();let signal:AbortSignal | undefined;
  mocked.mockImplementation(async(_path,options)=>new Promise((_resolve,reject)=>{signal=options?.signal as AbortSignal;signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')));}));
  const refresh=vi.fn(),view=render(<HostsPage hosts={[host()]} credentials={[]} runtime={runtime} hash="#/hosts" onRefresh={refresh} onCreateCredential={vi.fn()}/>);
  fireEvent.mouseEnter(screen.getByRole('button',{name:'alpha'}));await act(async()=>{vi.advanceTimersByTime(300);});
  view.unmount();await act(async()=>{});
  expect(signal?.aborted).toBe(true);expect(refresh).not.toHaveBeenCalled();
});

it('collects host info from details only after the explicit action',async()=>{
  const refresh=vi.fn().mockResolvedValue(undefined);
  mocked.mockImplementation(async(path,options)=>options?.method==='POST'?{alias:'alpha',ok:true,message:'Host info collected'}:path.endsWith('/workspace')?{paths:[],skills:[]}:{hops:[],blocked_by:null});
  render(<HostsPage hosts={[host()]} credentials={[]} runtime={runtime} hash="#/hosts/alpha" onRefresh={refresh} onCreateCredential={vi.fn()}/>);
  const dialog=screen.getByRole('dialog',{name:'Host details alpha'});
  expect(dialog).toHaveTextContent('Connection checks do not collect system info.');
  expect(mocked.mock.calls.every(([,options])=>!options?.method)).toBe(true);
  fireEvent.click(within(dialog).getByRole('button',{name:'Collect host info'}));
  await waitFor(()=>expect(refresh).toHaveBeenCalledOnce());
  expect(mocked.mock.calls.filter(([,options])=>options?.method==='POST')).toEqual([['/hosts/host-a/observe',expect.objectContaining({method:'POST'})]]);
});

it('collects only selected hosts in a bounded batch and shows partial failures',async()=>{
  let finish!:(value:unknown)=>void;mocked.mockImplementation(async()=>new Promise(done=>{finish=done;}));
  const refresh=vi.fn().mockResolvedValue(undefined),values=[host(),host({id:'host-b',alias:'beta'}),host({id:'host-c',alias:'gamma'})];
  render(<HostsPage hosts={values} credentials={[]} runtime={runtime} hash="#/hosts" onRefresh={refresh} onCreateCredential={vi.fn()}/>);
  fireEvent.click(screen.getByRole('checkbox',{name:'Select alpha'}));
  fireEvent.click(screen.getByRole('checkbox',{name:'Select beta'}));
  expect(mocked).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:'Collect host info'}));
  expect(screen.getAllByRole('button',{name:'Collecting…'})).toHaveLength(1);
  screen.getAllByRole('button',{name:'Collecting…'}).forEach(button=>expect(button).toBeDisabled());
  expect(screen.getByRole('button',{name:'Run checks'})).toBeDisabled();
  expect(mocked).toHaveBeenCalledExactlyOnceWith('/hosts/observe',expect.objectContaining({method:'POST',body:JSON.stringify({identifiers:['host-a','host-b'],timeout:8,concurrency:4})}));
  await act(async()=>finish({results:[{alias:'alpha',ok:true,message:'Host info collected'},{alias:'beta',ok:false,message:'Remote probe failed'}]}));
  const dialog=await screen.findByRole('dialog',{name:'Host info results'});
  expect(dialog).toHaveTextContent('alpha');expect(dialog).toHaveTextContent('beta');expect(dialog).toHaveTextContent('Remote probe failed');
  expect(dialog).not.toHaveTextContent('gamma');expect(refresh).toHaveBeenCalledOnce();
});

it('disables bulk collection above the runtime selection limit',()=>{
  render(<HostsPage hosts={[host(),host({id:'host-b',alias:'beta'})]} credentials={[]} runtime={{...runtime,capabilities:{...runtime.capabilities,max_bulk_hosts:1}}} hash="#/hosts" onRefresh={vi.fn()} onCreateCredential={vi.fn()}/>);
  fireEvent.click(screen.getByRole('checkbox',{name:'Select alpha'}));fireEvent.click(screen.getByRole('checkbox',{name:'Select beta'}));
  expect(screen.getByRole('button',{name:'Collect host info'})).toBeDisabled();
  expect(screen.getByRole('button',{name:'Run checks'})).toBeDisabled();
  expect(mocked).not.toHaveBeenCalled();
});

it('does not reveal masked metadata through hover or keyboard focus',async()=>{
  vi.useFakeTimers();
  localStorage.setItem('ssm.host-details','false');
  const value=host({hostname:'private-endpoint.invalid',username:'private-user',credential_label:'private-credential'});
  render(<HostsPage hosts={[value]} credentials={[]} runtime={runtime} hash="#/hosts" onRefresh={vi.fn()} onCreateCredential={vi.fn()}/>);
  fireEvent.mouseEnter(screen.getByRole('button',{name:'alpha'}));
  await act(async()=>{vi.advanceTimersByTime(300);});
  fireEvent.focus(screen.getByRole('button',{name:'alpha'}));
  expect(screen.queryByRole('tooltip')).toBeNull();
  expect(screen.queryByText(/private-endpoint|private-user|private-credential/)).toBeNull();
  expect(mocked).not.toHaveBeenCalled();
});

it.each(['visible','scheduled'])('clears a %s preview when details are hidden',async(state)=>{
  vi.useFakeTimers();
  render(<HostsPage hosts={[host()]} credentials={[]} runtime={runtime} hash="#/hosts" onRefresh={vi.fn()} onCreateCredential={vi.fn()}/>);
  fireEvent.mouseEnter(screen.getByRole('button',{name:'alpha'}));
  if(state==='visible')await act(async()=>{vi.advanceTimersByTime(300);});
  fireEvent.click(screen.getByRole('button',{name:'Hide details'}));
  expect(screen.queryByRole('tooltip')).toBeNull();
  await act(async()=>{vi.advanceTimersByTime(300);});
  fireEvent.click(screen.getByRole('button',{name:'Show details'}));
  expect(screen.queryByRole('tooltip')).toBeNull();
});

it('preserves native Enter activation on a focused row action',async()=>{
  mocked.mockResolvedValue({ok:true,message:'checked'});
  render(<HostsPage hosts={[host()]} credentials={[]} runtime={runtime} hash="#/hosts" onRefresh={vi.fn()} onCreateCredential={vi.fn()}/>);
  fireEvent.keyDown(document,{key:'ArrowDown'});
  screen.getByRole('button',{name:'Run check alpha'}).focus();
  await userEvent.keyboard('{Enter}');
  expect(mocked).toHaveBeenCalledWith('/hosts/host-a/test',expect.objectContaining({method:'POST'}));
  expect(location.hash).toBe('#/hosts');
});

it('ignores inventory shortcuts while host details or a browser shortcut is active',()=>{
  mocked.mockImplementation(async path=>path.endsWith('/workspace')?{paths:[],skills:[]}:{hops:[],blocked_by:null});
  const values=[host()],props={hosts:values,credentials:[],runtime,onRefresh:vi.fn(),onCreateCredential:vi.fn()};
  const view=render(<HostsPage {...props} hash="#/hosts"/>);
  fireEvent.keyDown(document,{key:'ArrowDown'});
  for(const modifier of ['metaKey','ctrlKey','altKey'])fireEvent.keyDown(document,{key:'t',[modifier]:true});
  expect(mocked).not.toHaveBeenCalled();
  view.rerender(<HostsPage {...props} hash="#/hosts/alpha"/>);
  fireEvent.keyDown(screen.getByRole('dialog'),{key:'t'});
  expect(mocked.mock.calls.every(([,options])=>!options?.method)).toBe(true);
});

it('keeps the keyboard cursor on a host identity and ignores a filtered-out host',()=>{
  const values=[host(),host({id:'host-b',alias:'beta',hostname:'beta.invalid'})],props={hosts:values,credentials:[],runtime,onRefresh:vi.fn(),onCreateCredential:vi.fn()};
  const view=render(<HostsPage {...props} hash="#/hosts"/>);
  fireEvent.keyDown(document,{key:'ArrowDown'});
  fireEvent.keyDown(document,{key:'ArrowDown'});
  view.rerender(<HostsPage {...props} hosts={[...values].reverse()} hash="#/hosts"/>);
  expect(document.querySelector('.host-row.cursor')).toHaveAttribute('data-host','beta');
  fireEvent.change(screen.getByRole('textbox',{name:'Search hosts'}),{target:{value:'alpha'}});
  const errors:ErrorEvent[]=[],capture=(event:ErrorEvent)=>{event.preventDefault();errors.push(event);};
  window.addEventListener('error',capture);
  try {
    fireEvent.keyDown(document,{key:'Enter'});
    fireEvent.keyDown(document,{key:'t'});
    expect(errors).toHaveLength(0);
    expect(location.hash).toBe('#/hosts');
    expect(mocked).not.toHaveBeenCalled();
    fireEvent.keyDown(document,{key:'ArrowDown'});
    expect(document.querySelector('.host-row.cursor')).toHaveAttribute('data-host','alpha');
  } finally {window.removeEventListener('error',capture);}
});

it.each(['diagnose','edit'])('preserves a previously pinned %s action',async pin=>{
  localStorage.setItem('ssm.host-pins',JSON.stringify([pin]));
  mocked.mockResolvedValue({alias:'alpha',checks:[]});
  render(<HostsPage hosts={[host()]} credentials={[]} runtime={runtime} hash="#/hosts" onRefresh={vi.fn()} onCreateCredential={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button',{name:pin==='diagnose'?'Run full diagnosis alpha':'Edit host alpha'}));
  if(pin==='diagnose')await waitFor(()=>expect(mocked).toHaveBeenCalledWith('/hosts/host-a/diagnose',expect.objectContaining({method:'POST'})));
  else {expect(screen.getByRole('dialog',{name:'Edit alpha'})).toBeVisible();expect(mocked).not.toHaveBeenCalled();}
});

it('ignores a late workspace response after switching hosts',async()=>{
  let resolve!:(value:unknown)=>void;const delayed=new Promise<unknown>(done=>resolve=done);
  mocked.mockImplementation(async path=>path.includes('host-a/workspace')?delayed:path.endsWith('/workspace')?{paths:[],skills:[]}: {hops:[{alias:path.includes('host-b')?'beta':'alpha',external:false}],blocked_by:null});
  const values=[host(),host({id:'host-b',alias:'beta'})],props={hosts:values,credentials:[],runtime,onRefresh:vi.fn(),onCreateCredential:vi.fn()};
  const view=render(<HostsPage {...props} hash="#/hosts/alpha"/>);view.rerender(<HostsPage {...props} hash="#/hosts/beta"/>);
  await act(async()=>resolve({paths:[{id:'wrong',label:'STALE DIRECTORY',path:'/wrong'}],skills:[]}));expect(screen.queryByText('STALE DIRECTORY')).toBeNull();expect(screen.getByRole('dialog',{name:'Host details beta'})).toBeTruthy();
});

it('preserves the CLI password onboarding and retries only a failed binding',async()=>{
  const value=host(),saved=credential({label:value.alias});let bindings=0;
  mocked.mockImplementation(async(path,options)=>{if(path==='/credentials'&&options?.method==='POST')return saved;if(path==='/hosts/host-a'&&options?.method==='PATCH'){if(++bindings===1)throw new Error('Configuration synchronization failed');return {...value,credential_id:saved.id};}throw new Error('Unexpected request');});
  render(<HostsPage hosts={[value]} credentials={[]} runtime={runtime} hash="#/servers/alpha/credential" onRefresh={vi.fn().mockResolvedValue(undefined)} onCreateCredential={vi.fn()}/>);
  const password=screen.getByLabelText('Password',{selector:'input'});expect(document.activeElement).toBe(password);
  expect(screen.getByLabelText('Label')).toHaveValue('alpha');fireEvent.change(password,{target:{value:'fixture-only'}});
  fireEvent.click(screen.getByRole('button',{name:'Save and use'}));await screen.findByRole('button',{name:'Retry binding'});
  expect(screen.getByRole('alert')).toHaveTextContent('Configuration synchronization failed');
  fireEvent.click(screen.getByRole('button',{name:'Retry binding'}));await waitFor(()=>expect(bindings).toBe(2));
  expect(mocked.mock.calls.filter(([path,options])=>path==='/credentials'&&options?.method==='POST')).toHaveLength(1);
  expect(localStorage.length).toBe(0);expect(location.hash).not.toContain('fixture-only');
});

it('requires a preview of the current SSH config before applying an import',async()=>{
  let finish!:(value:unknown)=>void;const pending=new Promise(done=>finish=done);
  mocked.mockImplementation(async()=>pending);
  render(<HostsPage hosts={[]} credentials={[]} runtime={runtime} hash="#/hosts" onRefresh={vi.fn()} onCreateCredential={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button',{name:'Import SSH config'}));const input=screen.getByLabelText('Config path · leave blank for default');
  fireEvent.change(input,{target:{value:'/config/A'}});fireEvent.click(screen.getByRole('button',{name:'Preview'}));expect(input).toBeDisabled();
  fireEvent.change(input,{target:{value:'/config/B'}});await act(async()=>finish({items:[],errors:[],skipped_patterns:[]}));
  expect(screen.getByRole('button',{name:'Import hosts'})).toBeDisabled();expect(mocked).toHaveBeenCalledTimes(1);
});

it('shows authentication fallback warnings before the user applies an import',async()=>{
  const message='Authentication settings are not converted to managed credentials. After import, this host uses /selected/config or an explicitly bound credential.';
  mocked.mockResolvedValue({config:'/selected/config',items:[],errors:[],skipped_patterns:[],warnings:[{alias:'alpha',code:'authentication-not-imported',message,source_config:'/selected/config',fallback_config:'/selected/config',identity_files:['~/.ssh/selected-key']}]});
  render(<HostsPage hosts={[]} credentials={[]} runtime={runtime} hash="#/hosts" onRefresh={vi.fn()} onCreateCredential={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button',{name:'Import SSH config'}));
  fireEvent.click(screen.getByRole('button',{name:'Preview'}));
  expect(await screen.findByText(`alpha: ${message}`)).toBeVisible();
  expect(screen.getByText('~/.ssh/selected-key')).toBeVisible();
  expect(screen.getAllByText('/selected/config')).toHaveLength(2);
  expect(mocked.mock.calls.map(([path])=>path)).toEqual(['/import/preview']);
});
