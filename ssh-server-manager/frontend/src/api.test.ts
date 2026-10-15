import {beforeEach,describe,expect,it,vi} from 'vitest';
import {api,ApiError,download,json,setCsrf,stream} from './api';
const fetchMock=vi.fn<typeof fetch>();
beforeEach(()=>{fetchMock.mockReset();vi.stubGlobal('fetch',fetchMock);setCsrf('test-csrf');});
describe('same-origin API client',()=>{
  it('protects mutations, preserves multipart boundaries and requests no-store cookies',async()=>{
    fetchMock.mockResolvedValue(new Response('{}'));const body=new FormData();body.append('file',new File(['data'],'payload.txt'));
    await api('/hosts/a/upload',{method:'POST',body});
    const [url,options]=fetchMock.mock.calls[0];expect(url).toBe('/api/v1/hosts/a/upload');expect(options?.credentials).toBe('same-origin');expect(options?.cache).toBe('no-store');
    const headers=new Headers(options?.headers);expect(headers.get('X-CSRF-Token')).toBe('test-csrf');expect(headers.has('Content-Type')).toBe(false);
  });
  it('normalizes business and HTTP errors without retrying writes',async()=>{
    fetchMock.mockResolvedValue(new Response(JSON.stringify({detail:'Permission denied'}),{status:403}));
    await expect(api('/hosts',json('POST',{}))).rejects.toMatchObject({status:403,message:'Permission denied'});expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it('signals session expiry once without replaying a request',async()=>{
    const expired=vi.fn();window.addEventListener('ssm-session-expired',expired);fetchMock.mockResolvedValue(new Response('{}',{status:401}));
    await expect(api('/hosts',json('POST',{}))).rejects.toBeInstanceOf(ApiError);expect(expired).toHaveBeenCalledTimes(1);expect(fetchMock).toHaveBeenCalledTimes(1);window.removeEventListener('ssm-session-expired',expired);
  });
  it('downloads binary content using POST and RFC5987 filenames',async()=>{
    fetchMock.mockResolvedValue(new Response('payload',{headers:{'Content-Disposition':"attachment; filename*=UTF-8''%E4%B8%AD%E6%96%87%20%23.txt"}}));
    const result=await download('/hosts/a/download',{path:'/中文 #.txt'});expect(result.filename).toBe('中文 #.txt');expect(await result.blob.text()).toBe('payload');expect(fetchMock.mock.calls[0][1]?.method).toBe('POST');
  });
  it('requires a final diagnostic result and cleans the stream',async()=>{
    fetchMock.mockResolvedValue(new Response('{"type":"stage","message":"Waiting"}\n'));await expect(stream('/credentials/a/test',{},vi.fn(),new AbortController().signal)).rejects.toThrow('before the test completed');
    fetchMock.mockResolvedValue(new Response('{"type":"stage","message":"Ready"}\n{"type":"result","result":{"ok":true}}\n'));const receive=vi.fn();await stream('/credentials/a/test',{},receive,new AbortController().signal);expect(receive).toHaveBeenCalledTimes(2);
  });
});
