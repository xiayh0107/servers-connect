import {act,fireEvent,render,screen,waitFor,within} from '@testing-library/react';
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {api,ApiError,stream} from '../api';
import {credential,host,runtime} from '../test-fixtures';
import type {RuntimeInfo} from '../types';
import CredentialsPage from './CredentialsPage';
vi.mock('../api',async original=>({...await original<typeof import('../api')>(),api:vi.fn(),stream:vi.fn()}));
const mocked=vi.mocked(api),mockedStream=vi.mocked(stream);
const observed='2026-10-15T12:00:00Z';
const keyMetadata={path:'/key',status:'valid' as const,format:'openssh' as const,encrypted:false,algorithm:'Ed25519',fingerprint:'SHA256:fixture',bits:256,permissions:{method:'posix_mode' as const,assessment:'secure' as const,mode:'0600'},observed_at:observed};
beforeEach(()=>{mocked.mockReset();mockedStream.mockReset();history.replaceState({},'','#/credentials');localStorage.clear();mocked.mockImplementation(async path=>path.startsWith('/credential-deletions')?[]:Promise.reject(new Error(`Unexpected ${path}`)));});
afterEach(()=>{vi.useRealTimers();delete (document as unknown as Record<string,unknown>).hidden;});
const props=(value=credential(),runtimeValue:RuntimeInfo=runtime)=>({hosts:[host({credential_id:value.id})],credentials:[value],runtime:runtimeValue,onRefresh:vi.fn(async()=>{}),onRuntimeRefresh:vi.fn(async()=>{})});

it('does not read a key or contact an agent/vault merely by opening the page',async()=>{
  render(<CredentialsPage {...props(credential({kind:'key',key_path:'/key',has_secret:false,key_metadata:null}))}/>);await waitFor(()=>expect(mocked).toHaveBeenCalled());
  expect(mocked.mock.calls.map(([path])=>path)).toEqual(['/credential-deletions?pending=true']);
  expect(screen.getByText(/Not checked/)).toBeTruthy();
  expect(screen.getByText('No passphrase stored')).toBeTruthy();
});

it('clears validated key metadata immediately when the key path changes',async()=>{
  const key=credential({kind:'key',key_path:'/key',has_secret:false,key_metadata:keyMetadata});
  render(<CredentialsPage {...props(key)}/>);
  expect(screen.getByText(/File found · 0600/)).toBeTruthy();
  fireEvent.change(screen.getByLabelText('Private key path'),{target:{value:'/other'}});
  expect(screen.queryByText(/File found · 0600/)).toBeNull();
  expect(screen.getByText(/Not checked/)).toBeTruthy();
  expect(mocked.mock.calls.map(([path])=>path)).toEqual(['/credential-deletions?pending=true']);
});

it('masks a stored passphrase with twelve dots and keeps the vault notice verbatim',()=>{
  const key=credential({kind:'key',key_path:'/key',has_secret:false,has_passphrase:true,key_metadata:keyMetadata});
  render(<CredentialsPage {...props(key,{...runtime,vault:{name:'macOS Keychain',status:'ready'}})}/>);
  expect(screen.getByText('••••••••••••')).toBeTruthy();
  expect(screen.getByText('Stored in macOS Keychain. Never shown in the browser or sent to agents.')).toBeTruthy();
});

it('keeps the Type control an editable dropdown while credential types stay fixed',()=>{
  render(<CredentialsPage {...props()}/>);
  const type=screen.getByLabelText('Type');
  expect(type).toHaveValue('password');
  fireEvent.change(type,{target:{value:'agent'}});
  expect(type).toHaveValue('password');
  expect(screen.getByText(/fixed after creation/)).toBeTruthy();
});

it('saves a label change with the credential version it was rendered from',async()=>{
  const value=credential({label:'Deploy password'}),updated={...value,label:'Renamed',updated_at:'2026-10-15T13:00:00Z'};
  mocked.mockImplementation(async(path,options)=>{if(path.startsWith('/credential-deletions'))return [];if(path==='/credentials/credential-a'&&options?.method==='PATCH')return updated;throw new Error('Unexpected');});
  const view=props(value);render(<CredentialsPage {...view}/>);
  fireEvent.change(screen.getByLabelText('Label'),{target:{value:'Renamed'}});
  expect(screen.getByText('1 unsaved change')).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'Save credential'}));
  await waitFor(()=>expect(mocked.mock.calls.some(([,options])=>options?.method==='PATCH')).toBe(true));
  const call=mocked.mock.calls.find(([,options])=>options?.method==='PATCH')!;
  expect(JSON.parse(String(call[1]?.body))).toEqual({expected_updated_at:value.updated_at,label:'Renamed'});
  expect(view.onRefresh).toHaveBeenCalled();
});

it('adds a key credential through the Paper type cards and reports unsafe key permissions',async()=>{
  const unsafe={...keyMetadata,permissions:{method:'posix_mode' as const,assessment:'unsafe' as const,mode:'0644'}};
  mocked.mockImplementation(async(path,options)=>{if(path.startsWith('/credential-deletions'))return [];if(path==='/credentials/key/inspect')return {key_metadata:unsafe,key_revision:'rev-1'};if(path==='/credentials'&&options?.method==='POST')return credential({id:'credential-new',label:'ci-runner',kind:'key',key_path:'/key',has_secret:false});throw new Error(`Unexpected ${path}`);});
  render(<CredentialsPage {...props()}/>);
  fireEvent.click(screen.getByRole('button',{name:'Add credential'}));
  const dialog=screen.getByRole('dialog',{name:'Add credential'});
  expect(within(dialog).getByRole('radio',{name:/Private key file/})).toBeChecked();
  expect(within(dialog).getByText('Esc to cancel · ⌘↵ to save')).toBeTruthy();
  fireEvent.change(within(dialog).getByLabelText('Label'),{target:{value:'ci-runner'}});
  fireEvent.change(within(dialog).getByLabelText('Private key path'),{target:{value:'/key'}});
  fireEvent.change(within(dialog).getByLabelText('Passphrase · optional'),{target:{value:'fixture-passphrase'}});
  fireEvent.keyDown(within(dialog).getByLabelText('Passphrase · optional'),{key:'Enter',metaKey:true});
  await waitFor(()=>expect(mocked.mock.calls.some(([path])=>path==='/credentials/key/inspect')).toBe(true));
  const post=mocked.mock.calls.find(([path,options])=>path==='/credentials'&&options?.method==='POST')!;
  expect(JSON.parse(String(post[1]?.body))).toEqual({kind:'key',label:'ci-runner',key_path:'/key',passphrase:'fixture-passphrase',expected_key_revision:'rev-1'});
});

it('requires the exact label before deleting and explains the fallback',async()=>{
  const value=credential({label:'deploy-ed25519'});
  mocked.mockImplementation(async path=>{if(path.startsWith('/credential-deletions'))return [];if(path.endsWith('/deletion-preview'))return {credential:value,expected_updated_at:'version-1',expected_host_ids:['host-a'],hosts:[{id:'host-a',alias:'alpha'}]};throw new Error(`Unexpected ${path}`);});
  render(<CredentialsPage {...props(value)}/>);
  fireEvent.click(screen.getByRole('button',{name:'Delete'}));
  const dialog=screen.getByRole('dialog',{name:'Delete “deploy-ed25519”?'});
  await within(dialog).findByText(/1 host uses this credential/);
  expect(within(dialog).getByText(/This can't be undone\./)).toBeTruthy();
  const del=within(dialog).getByRole('button',{name:'Delete credential'});
  expect(del).toBeDisabled();
  fireEvent.change(within(dialog).getByLabelText('Type the label to confirm'),{target:{value:'deploy'}});
  expect(del).toBeDisabled();
  fireEvent.change(within(dialog).getByLabelText('Type the label to confirm'),{target:{value:'deploy-ed25519'}});
  expect(del).not.toBeDisabled();
});

it('requires fresh confirmation after deletion references change',async()=>{
  const value=credential();let preview=0;mocked.mockImplementation(async(path,options)=>{if(path.startsWith('/credential-deletions'))return [];if(path.endsWith('/deletion-preview')){preview++;return {credential:value,expected_updated_at:`version-${preview}`,expected_host_ids:['host-a'],hosts:[{id:'host-a',alias:'alpha'}]};}if(path.endsWith('/detach-delete'))throw new ApiError(409,'ConflictError','References changed');throw new Error(`Unexpected ${options?.method} ${path}`);});
  render(<CredentialsPage {...props(value)}/>);
  fireEvent.click(screen.getByRole('button',{name:'Delete'}));
  const dialog=screen.getByRole('dialog',{name:'Delete “Deploy password”?'});
  await within(dialog).findByText(/1 host uses this credential/);
  fireEvent.change(within(dialog).getByLabelText('Type the label to confirm'),{target:{value:value.label}});
  fireEvent.click(within(dialog).getByRole('button',{name:'Delete credential'}));
  await screen.findByRole('alert');
  expect(screen.getByRole('button',{name:'Delete credential'})).toBeDisabled();
  fireEvent.click(screen.getByRole('button',{name:'Refresh references'}));
  await waitFor(()=>expect(preview).toBe(2));
  expect(JSON.parse(String(mocked.mock.calls.find(([path])=>path.endsWith('/detach-delete'))![1]?.body))).toMatchObject({expected_updated_at:'version-1',expected_host_ids:['host-a']});
});

it('runs the three-step credential test and shows the ssh-copy-id remedy after a rejected key',async()=>{
  const value=credential({label:'deploy-ed25519',kind:'key',key_path:'/key',has_secret:false,key_metadata:keyMetadata});
  mockedStream.mockImplementation(async(_path,_body,onEvent)=>{onEvent({type:'stage',stage:'authenticate',status:'failed',message:'The selected credential was not accepted.'});onEvent({type:'result',result:{ok:false,error_code:'authentication-failed',message:'The selected credential was not accepted.',latency_ms:412,checked_at:observed,host_id:'host-a',host_alias:'alpha',credential_id:'credential-a',method:'publickey',stages:[]}});});
  render(<CredentialsPage {...props(value)}/>);
  fireEvent.click(screen.getByRole('button',{name:'Test on host…'}));
  const dialog=screen.getByRole('dialog',{name:'Test deploy-ed25519'});
  expect(within(dialog).getByText('TCP connect alpha.invalid:22')).toBeTruthy();
  expect(within(dialog).getByText('Verify host key against known_hosts')).toBeTruthy();
  fireEvent.click(within(dialog).getByRole('button',{name:'Run test'}));
  await within(dialog).findByText('Failed after 0.4 s');
  expect(within(dialog).getByText('Public key rejected for user “deploy”')).toBeTruthy();
  expect(within(dialog).getByText(/~deploy\/\.ssh\/authorized_keys/)).toBeTruthy();
  expect(within(dialog).getByText('ssh-copy-id -i /key.pub deploy@alpha.invalid')).toBeTruthy();
});

it('allows testing an unbound credential without mutating its host binding',async()=>{
  const value=credential({id:'unbound'});
  mockedStream.mockResolvedValue(undefined);
  render(<CredentialsPage hosts={[host({credential_id:null})]} credentials={[value]} runtime={runtime} onRefresh={vi.fn(async()=>{})} onRuntimeRefresh={vi.fn(async()=>{})}/>);
  expect(screen.getByRole('button',{name:'Test on host…'})).toBeEnabled();
  fireEvent.click(screen.getByRole('button',{name:'Test on host…'}));
  expect(screen.getByRole('dialog',{name:'Test Deploy password'})).toBeVisible();
  expect(mockedStream).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:'Run test'}));
  await waitFor(()=>expect(mockedStream).toHaveBeenCalledWith('/credentials/unbound/test',{host_id:'host-a',timeout:8},expect.any(Function),expect.any(AbortSignal)));
  expect(mocked.mock.calls.every(([,options])=>!options?.method)).toBe(true);
});

it('disables credential testing only when no saved hosts exist',()=>{
  render(<CredentialsPage {...props()} hosts={[]}/>);
  expect(screen.getByRole('button',{name:'Test on host…'})).toBeDisabled();
});

it.each(['timeout','ssh-failed','vault-unavailable','credential-secret-missing','credential-not-used','authentication-unconfirmed'])('does not infer successful route stages from %s',async code=>{
  const value=credential({id:`credential-${code}`});
  mockedStream.mockImplementation(async(_path,_body,onEvent)=>onEvent({type:'result',result:{ok:false,error_code:code,message:`Failure: ${code}`,latency_ms:10,checked_at:observed,host_id:'host-a',host_alias:'alpha',credential_id:value.id,method:null,stages:[]}}));
  render(<CredentialsPage {...props(value)}/>);
  fireEvent.click(screen.getByRole('button',{name:'Test on host…'}));
  const dialog=screen.getByRole('dialog',{name:`Test ${value.label}`});
  fireEvent.click(within(dialog).getByRole('button',{name:'Run test'}));
  await within(dialog).findByText(`Failure: ${code}`);
  expect(dialog.querySelectorAll('.test-step.ok')).toHaveLength(0);
  expect(dialog.querySelectorAll('.test-step.unknown')).toHaveLength(3);
  expect(within(dialog).queryByText(/rejected for user/)).toBeNull();
  expect(within(dialog).queryByText(/ssh-copy-id/)).toBeNull();
  expect(within(dialog).getByText(/did not confirm authentication|Resolve the reported error/)).toBeVisible();
});

it.each([
  {code:'connection-refused',tones:['failed','pending','pending']},
  {code:'host-key-untrusted',tones:['unknown','failed','pending']},
  {code:'authentication-failed',tones:['unknown','unknown','failed']},
])('marks only the established failure for $code',async({code,tones})=>{
  mockedStream.mockImplementation(async(_path,_body,onEvent)=>onEvent({type:'result',result:{ok:false,error_code:code,message:`Failure: ${code}`,latency_ms:10,checked_at:observed,host_id:'host-a',host_alias:'alpha',credential_id:'credential-a',method:null,stages:[]}}));
  render(<CredentialsPage {...props()}/>);
  fireEvent.click(screen.getByRole('button',{name:'Test on host…'}));
  const dialog=screen.getByRole('dialog',{name:'Test Deploy password'});
  fireEvent.click(within(dialog).getByRole('button',{name:'Run test'}));
  await within(dialog).findByText(`Failure: ${code}`);
  within(dialog).getAllByRole('listitem').forEach((step,index)=>expect(step).toHaveClass(tones[index]));
  expect(dialog.querySelectorAll('.test-step.ok')).toHaveLength(0);
});

it('does not blame a target key for an unidentified jump-host authentication failure',async()=>{
  const value=credential({kind:'key',key_path:'/key',has_secret:false});
  mockedStream.mockImplementation(async(_path,_body,onEvent)=>onEvent({type:'result',result:{ok:false,error_code:'authentication-failed',message:'The selected credential was not accepted.',latency_ms:10,checked_at:observed,host_id:'host-a',host_alias:'alpha',credential_id:value.id,method:null,stages:[]}}));
  render(<CredentialsPage {...props(value)} hosts={[host({credential_id:value.id,proxy_jumps:['jump']})]}/>);
  fireEvent.click(screen.getByRole('button',{name:'Test on host…'}));
  fireEvent.click(screen.getByRole('button',{name:'Run test'}));
  await screen.findByText('Authentication rejected on the SSH route');
  expect(screen.getByText(/rejected hop was not identified/)).toBeVisible();
  expect(screen.queryByText(/ssh-copy-id/)).toBeNull();
});

it('marks all checkpoints passed only after confirmed authentication',async()=>{
  mockedStream.mockImplementation(async(_path,_body,onEvent)=>onEvent({type:'result',result:{ok:true,error_code:null,message:'The selected credential was accepted.',latency_ms:10,checked_at:observed,host_id:'host-a',host_alias:'alpha',credential_id:'credential-a',method:'password',stages:[]}}));
  render(<CredentialsPage {...props()}/>);
  fireEvent.click(screen.getByRole('button',{name:'Test on host…'}));
  const dialog=screen.getByRole('dialog',{name:'Test Deploy password'});
  expect(dialog.querySelectorAll('.test-step.pending')).toHaveLength(3);
  expect(mockedStream).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole('button',{name:'Run test'}));
  await within(dialog).findByText('Verified after 10 ms');
  expect(dialog.querySelectorAll('.test-step.ok')).toHaveLength(3);
  expect(within(dialog).getByText('Host key matches known_hosts')).toBeVisible();
});

it('consumes one reveal grant, then clears the secret when the page is hidden',async()=>{
  mocked.mockImplementation(async path=>path.startsWith('/credential-deletions')?[]:path.endsWith('/master/verify')?{ok:true}:path.endsWith('/reveal')?{value:'synthetic-test-value'}:Promise.reject(new Error('Unexpected')));
  render(<CredentialsPage {...props()}/>);fireEvent.click(screen.getByRole('button',{name:'Reveal'}));const dialog=screen.getByRole('dialog');fireEvent.change(within(dialog).getByLabelText('Master password'),{target:{value:'test-master-password'}});fireEvent.click(within(dialog).getByRole('button',{name:'Authenticate and reveal'}));
  await screen.findByText('synthetic-test-value');expect(mocked.mock.calls.filter(([path])=>path.endsWith('/reveal'))).toHaveLength(1);expect(localStorage.length).toBe(0);
  await act(async()=>{document.dispatchEvent(new Event('visibilitychange'));Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'));});expect(screen.queryByText('synthetic-test-value')).toBeNull();Object.defineProperty(document,'hidden',{configurable:true,value:false});
});

it('discards a pending reveal response after the page is hidden and does not reveal on return',async()=>{
  Object.defineProperty(document,'hidden',{configurable:true,value:false});
  let complete!:(value:{value:string})=>void;
  const pending=new Promise<{value:string}>(resolve=>{complete=resolve;});
  mocked.mockImplementation(async path=>path.startsWith('/credential-deletions')?[]:path==='/auth/master/verify'?{ok:true}:path.endsWith('/reveal')?pending:Promise.reject(new Error(`Unexpected ${path}`)));
  render(<CredentialsPage {...props()}/>);
  fireEvent.click(screen.getByRole('button',{name:'Reveal'}));
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

it('removes a revealed secret after fifteen seconds without renewing the grant',async()=>{
  vi.useFakeTimers();
  mocked.mockImplementation(async path=>path.startsWith('/credential-deletions')?[]:path.endsWith('/master/verify')?{ok:true}:path.endsWith('/reveal')?{value:'synthetic-expiring-value'}:Promise.reject(new Error('Unexpected')));
  render(<CredentialsPage {...props()}/>);fireEvent.click(screen.getByRole('button',{name:'Reveal'}));const dialog=screen.getByRole('dialog');fireEvent.change(within(dialog).getByLabelText('Master password'),{target:{value:'synthetic-master'}});
  await act(async()=>{fireEvent.click(within(dialog).getByRole('button',{name:'Authenticate and reveal'}));});
  expect(screen.getByText('synthetic-expiring-value')).toBeTruthy();
  await act(async()=>vi.advanceTimersByTime(15_001));
  expect(screen.queryByText('synthetic-expiring-value')).toBeNull();expect(mocked.mock.calls.filter(([path])=>path.endsWith('/reveal'))).toHaveLength(1);
});
