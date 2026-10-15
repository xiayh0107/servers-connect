import {act,fireEvent,render,screen,waitFor,within} from '@testing-library/react';
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {api,ApiError} from '../api';
import {credential,host,runtime} from '../test-fixtures';
import CredentialsPage from './CredentialsPage';
vi.mock('../api',async original=>({...await original<typeof import('../api')>(),api:vi.fn()}));
const mocked=vi.mocked(api);
beforeEach(()=>{mocked.mockReset();history.replaceState({},'','#/credentials');localStorage.clear();mocked.mockImplementation(async path=>path.startsWith('/credential-deletions')?[]:Promise.reject(new Error(`Unexpected ${path}`)));});
afterEach(()=>{vi.useRealTimers();delete (document as unknown as Record<string,unknown>).hidden;});
const props=(value=credential())=>({hosts:[host({credential_id:value.id})],credentials:[value],runtime,onRefresh:vi.fn(async()=>{}),onRuntimeRefresh:vi.fn(async()=>{})});
it('does not read a key or contact an agent/vault merely by opening the page',async()=>{
  render(<CredentialsPage {...props(credential({kind:'key',key_path:'/key',has_secret:false}))}/>);await waitFor(()=>expect(mocked).toHaveBeenCalled());
  expect(mocked.mock.calls.map(([path])=>path)).toEqual(['/credential-deletions?pending=true']);expect(screen.getByText(/Key metadata is unknown/)).toBeTruthy();
});
it('clears validated key metadata immediately when path or passphrase changes',async()=>{
  const key=credential({kind:'key',key_path:'/key',has_secret:false,key_metadata:{path:'/key',status:'valid',format:'openssh',encrypted:false,algorithm:'Ed25519',fingerprint:'SHA256:fixture',bits:256,permissions:{method:'posix_mode',assessment:'secure',mode:'0600'},observed_at:'2026-10-15T12:00:00Z'}});
  render(<CredentialsPage {...props(key)}/>);expect(screen.getByText('Key parsed')).toBeTruthy();fireEvent.change(screen.getByLabelText('Private key path'),{target:{value:'/other'}});expect(screen.queryByText('Key parsed')).toBeNull();expect(screen.getByText(/Key metadata is unknown/)).toBeTruthy();
});
it('consumes one reveal grant, then clears the secret when the page is hidden',async()=>{
  mocked.mockImplementation(async path=>path.startsWith('/credential-deletions')?[]:path.endsWith('/master/verify')?{ok:true}:path.endsWith('/reveal')?{value:'synthetic-test-value'}:Promise.reject(new Error('Unexpected')));
  render(<CredentialsPage {...props()}/>);fireEvent.click(screen.getByRole('button',{name:'Reveal password…'}));const dialog=screen.getByRole('dialog');fireEvent.change(within(dialog).getByLabelText('Master password'),{target:{value:'test-master-password'}});fireEvent.click(within(dialog).getByRole('button',{name:'Authenticate and reveal'}));
  await screen.findByText('synthetic-test-value');expect(mocked.mock.calls.filter(([path])=>path.endsWith('/reveal'))).toHaveLength(1);expect(localStorage.length).toBe(0);
  await act(async()=>{document.dispatchEvent(new Event('visibilitychange'));Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'));});expect(screen.queryByText('synthetic-test-value')).toBeNull();Object.defineProperty(document,'hidden',{configurable:true,value:false});
});
it('discards a pending reveal response after the page is hidden and does not reveal on return',async()=>{
  Object.defineProperty(document,'hidden',{configurable:true,value:false});
  let complete!:(value:{value:string})=>void;
  const pending=new Promise<{value:string}>(resolve=>{complete=resolve;});
  mocked.mockImplementation(async path=>path.startsWith('/credential-deletions')?[]:path==='/auth/master/verify'?{ok:true}:path.endsWith('/reveal')?pending:Promise.reject(new Error(`Unexpected ${path}`)));
  render(<CredentialsPage {...props()}/>);
  fireEvent.click(screen.getByRole('button',{name:'Reveal password…'}));
  const dialog=screen.getByRole('dialog');
  fireEvent.change(within(dialog).getByLabelText('Master password'),{target:{value:'synthetic-review-password'}});
  fireEvent.click(within(dialog).getByRole('button',{name:'Authenticate and reveal'}));
  await waitFor(()=>expect(mocked.mock.calls.filter(([path])=>path.endsWith('/reveal'))).toHaveLength(1));
  await act(async()=>{
    Object.defineProperty(document,'hidden',{configurable:true,value:true});
    document.dispatchEvent(new Event('visibilitychange'));
  });
  expect(screen.queryByRole('dialog')).toBeNull();
  await act(async()=>complete({value:'synthetic-late-secret'}));
  expect(screen.queryByText('synthetic-late-secret')).toBeNull();
  await act(async()=>{
    Object.defineProperty(document,'hidden',{configurable:true,value:false});
    document.dispatchEvent(new Event('visibilitychange'));
  });
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(screen.queryByText('synthetic-late-secret')).toBeNull();
  expect(mocked.mock.calls.filter(([path])=>path.endsWith('/reveal'))).toHaveLength(1);
  expect(localStorage.length).toBe(0);
  await waitFor(()=>expect(new URLSearchParams(location.hash.split('?')[1]||'').getAll('dialog')).not.toContain('credential-reveal'));
});
it('requires fresh confirmation after deletion references change',async()=>{
  const value=credential();let preview=0;mocked.mockImplementation(async(path,options)=>{if(path.startsWith('/credential-deletions'))return [];if(path.endsWith('/deletion-preview')){preview++;return {credential:value,expected_updated_at:`version-${preview}`,expected_host_ids:['host-a'],hosts:[{id:'host-a',alias:'alpha'}]};}if(path.endsWith('/detach-delete'))throw new ApiError(409,'ConflictError','References changed');throw new Error(`Unexpected ${options?.method} ${path}`);});
  render(<CredentialsPage {...props(value)}/>);fireEvent.click(screen.getByRole('button',{name:'Delete…'}));await screen.findByText('alpha',{selector:'.checklist strong'});fireEvent.click(screen.getByRole('button',{name:'Detach and delete'}));await screen.findByRole('alert');expect(screen.getByRole('button',{name:'Detach and delete'})).toBeDisabled();fireEvent.click(screen.getByRole('button',{name:'Refresh confirmation'}));await waitFor(()=>expect(preview).toBe(2));
  expect(JSON.parse(String(mocked.mock.calls.find(([path])=>path.endsWith('/detach-delete'))![1]?.body))).toMatchObject({expected_updated_at:'version-1',expected_host_ids:['host-a']});
});

it('removes a revealed secret after fifteen seconds without renewing the grant',async()=>{
  vi.useFakeTimers();
  mocked.mockImplementation(async path=>path.startsWith('/credential-deletions')?[]:path.endsWith('/master/verify')?{ok:true}:path.endsWith('/reveal')?{value:'synthetic-expiring-value'}:Promise.reject(new Error('Unexpected')));
  render(<CredentialsPage {...props()}/>);fireEvent.click(screen.getByRole('button',{name:'Reveal password…'}));const dialog=screen.getByRole('dialog');fireEvent.change(within(dialog).getByLabelText('Master password'),{target:{value:'synthetic-master'}});
  await act(async()=>{fireEvent.click(within(dialog).getByRole('button',{name:'Authenticate and reveal'}));});
  expect(screen.getByText('synthetic-expiring-value')).toBeTruthy();
  await act(async()=>vi.advanceTimersByTime(15_001));
  expect(screen.queryByText('synthetic-expiring-value')).toBeNull();expect(mocked.mock.calls.filter(([path])=>path.endsWith('/reveal'))).toHaveLength(1);
});
