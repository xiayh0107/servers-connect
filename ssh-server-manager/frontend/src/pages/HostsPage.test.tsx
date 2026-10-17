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
  const value=host({facts:{os_logo:'ubuntu',os_pretty:'Ubuntu 24.04 LTS',arch:'x86_64',cpu_cores:'8',cpu_model:'AMD EPYC 7B13',memory:'Mem: 30.5Gi 11.2Gi 19.3Gi 0B 0B',disk:'/dev/nvme0n1p1 460G 120G 340G 27% /',load:'0.42 0.38 0.35',uptime:'up 14 days, 3 hours',observed_at:'2026-10-15T12:00:00Z'}});
  render(<HostsPage hosts={[value]} credentials={[]} runtime={runtime} hash="#/hosts" onRefresh={vi.fn()} onCreateCredential={vi.fn()}/>);
  const row=screen.getByRole('row',{name:/alpha/});
  fireEvent.mouseEnter(row);
  await act(async()=>{vi.advanceTimersByTime(300);});
  const preview=screen.getByRole('tooltip');
  expect(preview).toHaveTextContent('Runtime');
  expect(preview).toHaveTextContent('30.5 GiB');
  expect(preview).toHaveTextContent('8 vCPU');
  expect(preview).toHaveTextContent('37% used');
  fireEvent.mouseLeave(row);
  await act(async()=>{vi.advanceTimersByTime(200);});
  expect(screen.queryByRole('tooltip')).toBeNull();
  expect(mocked).not.toHaveBeenCalled();
  vi.useRealTimers();
});

it('points missing OS info at explicit collection instead of a connection check',async()=>{
  const value=host({last_test_at:'2026-10-15T12:00:00Z',last_test_status:'ok'}),refresh=vi.fn().mockResolvedValue(undefined);
  mocked.mockResolvedValue({alias:value.alias,ok:true,message:'Host info collected'});
  const props={hosts:[value],credentials:[],runtime,hash:'#/hosts',onRefresh:refresh,onCreateCredential:vi.fn()};
  const view=render(<HostsPage {...props}/>);
  expect(screen.queryByText(/run a check|Check to detect OS/i)).toBeNull();
  expect(mocked).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:'Collect host info alpha'}));
  await waitFor(()=>expect(refresh).toHaveBeenCalledOnce());
  expect(mocked).toHaveBeenCalledExactlyOnceWith('/hosts/host-a/observe',expect.objectContaining({method:'POST',body:JSON.stringify({timeout:8})}));
  view.rerender(<HostsPage {...props} hosts={[{...value,facts:{os_pretty:'Ubuntu 24.04 LTS',os_logo:'ubuntu',observed_at:'2026-10-17T12:00:00Z'}}]}/>);
  expect(screen.getByText('Ubuntu 24.04 LTS')).toBeVisible();
  expect(screen.queryByText('OS unknown')).toBeNull();
  expect(screen.queryByRole('button',{name:'Collect host info alpha'})).toBeNull();
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
  expect(screen.getByRole('button',{name:'Collecting…'})).toBeDisabled();
  expect(screen.getByRole('button',{name:'Run checks'})).toBeDisabled();
  expect(screen.getByRole('button',{name:'Collect host info alpha'})).toBeDisabled();
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
  fireEvent.mouseEnter(screen.getByRole('row',{name:/alpha/}));
  await act(async()=>{vi.advanceTimersByTime(300);});
  fireEvent.focus(screen.getByRole('button',{name:'alpha'}));
  expect(screen.queryByRole('tooltip')).toBeNull();
  expect(screen.queryByText(/private-endpoint|private-user|private-credential/)).toBeNull();
  expect(mocked).not.toHaveBeenCalled();
});

it.each(['visible','scheduled'])('clears a %s preview when details are hidden',async(state)=>{
  vi.useFakeTimers();
  render(<HostsPage hosts={[host()]} credentials={[]} runtime={runtime} hash="#/hosts" onRefresh={vi.fn()} onCreateCredential={vi.fn()}/>);
  fireEvent.mouseEnter(screen.getByRole('row',{name:/alpha/}));
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
