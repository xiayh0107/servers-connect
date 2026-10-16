import {act,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {beforeEach,expect,it,vi} from 'vitest';
import {api} from '../api';
import {credential,host,runtime} from '../test-fixtures';
import HostsPage,{visibleHostText} from './HostsPage';
vi.mock('../api',async original=>({...await original<typeof import('../api')>(),api:vi.fn()}));
const mocked=vi.mocked(api);
beforeEach(()=>{mocked.mockReset();history.replaceState({},'','#/hosts');localStorage.clear();});
it('opens local workspace and route without an implicit remote operation',async()=>{
  const value=host();mocked.mockImplementation(async path=>path.endsWith('/workspace')?{paths:[],skills:[]}:path.endsWith('/route')?{hops:[{alias:value.alias,external:false}],blocked_by:null}:Promise.reject(new Error('Unexpected remote action')));
  render(<HostsPage hosts={[value]} credentials={[]} runtime={runtime} hash="#/hosts/alpha" onRefresh={vi.fn()} onCreateCredential={vi.fn()}/>);
  await screen.findAllByText('Not observed');await waitFor(()=>expect(mocked.mock.calls.some(([path])=>path.endsWith('/route'))).toBe(true));
  expect(mocked.mock.calls.every(([path,options])=>!options?.method&&(/workspace$|route$/).test(path))).toBe(true);
});
it('does not render or search hidden endpoint, credential or note values',()=>{
  const value=host({hostname:'private-endpoint.invalid',notes:'private-note',credential_label:'private-credential'});
  expect(visibleHostText(value,false)).not.toContain('private');localStorage.setItem('ssm.host-details','false');
  render(<HostsPage hosts={[value]} credentials={[]} runtime={runtime} hash="#/hosts" onRefresh={vi.fn()} onCreateCredential={vi.fn()}/>);
  expect(screen.queryByText(/private-endpoint/)).toBeNull();expect(screen.queryByText('private-credential')).toBeNull();
  fireEvent.change(screen.getByRole('textbox',{name:/Search hosts/}),{target:{value:'private-note'}});expect(screen.queryByRole('button',{name:'alpha'})).toBeNull();
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
