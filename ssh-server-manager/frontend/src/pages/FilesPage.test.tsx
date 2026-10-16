import {act, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {api, ApiError, download} from '../api';
import type {DirectoryEntry, DirectoryListing, Host, RuntimeInfo, SavedPath} from '../types';
import FilesPage from './FilesPage';

vi.mock('../api', async importOriginal => ({...await importOriginal<typeof import('../api')>(), api: vi.fn(), download: vi.fn()}));
const apiMock = vi.mocked(api), downloadMock = vi.mocked(download);
const hosts: Host[] = ['alpha', 'beta'].map((alias, index) => ({
  id: index === 0 ? 'a/#' : 'b', alias, hostname: `${alias}.invalid`, username: 'deploy', port: 22,
  credential_id: null, credential_kind: null, credential_label: null, proxy_jumps: index ? ['bastion'] : [],
  tags: [], notes: '', source: 'manual', last_test_at: null, last_test_status: null, last_test_error_code: null,
  last_test_latency_ms: null, last_test_message: null, created_at: '', updated_at: '', address_scope: 'unresolved', jump_for_count: 0,
}));
const runtime: RuntimeInfo = {api_version: '1', version: '0.9.0', ui: 'modern', shell: 'web', csrf: '', server_time: '2026-10-15T12:00:00Z',
  session: {expires_at: '2026-10-15T20:00:00Z'}, vault: {name: 'Fixture vault', status: 'ready'},
  auth: {master_password_enrolled: false, passkeys: 0, webauthn_available: false},
  capabilities: {bulk_checks: true, credentials: true, file_conflicts: true, files: true, host_routes: true, hosts: true,
    skills: true, credential_detach_delete: true, credential_probe: true, credential_test_history: true, local_key_browser: true,
    private_key_validation: true, ssh_agent_identities: true, native_dialogs: false, native_window: false,
    max_bulk_hosts: 64, max_check_concurrency: 4, max_web_transfer_bytes: 100 * 1024 * 1024},
};
function entry(name: string, changes: Partial<DirectoryEntry> = {}): DirectoryEntry {
  return {name, path: `/srv/app/${name}`, type: 'file', size: 3, modified: 'Oct 14 12:00', permissions: '-rw-r--r--',
    owner: 'deploy', group: 'deploy', hidden: false, modified_at: null, uid: 1001, gid: 1001, link_target: null, ...changes};
}
function listing(changes: Partial<DirectoryListing> = {}): DirectoryListing {
  const entries = [entry('readme.txt')];
  return {alias: 'alpha', path: '/srv/app', parent: '/srv', entries, total: entries.length, matching_total: entries.length,
    truncated: false, unparsed: 0, latency_ms: 20, connection_checked_at: '2026-10-15T12:00:00Z', ...changes};
}
function saved(changes: Partial<SavedPath> = {}): SavedPath {
  return {id: 'saved/#', server: 'alpha', label: 'App logs', path: '/var/log/a #&?中', notes: 'Logs', created_at: '', updated_at: '', last_used_at: null, ...changes};
}
function mockBackend(files = listing(), paths: SavedPath[] = []) {
  apiMock.mockImplementation(async (path, options) => {
    if (path.includes('/files')) return files;
    if (path.endsWith('/paths') && !options?.method) return paths;
    if (path.endsWith('/upload')) return {bytes: 3};
    throw new Error(`Unexpected request: ${options?.method || 'GET'} ${path}`);
  });
}
function deferred<T>() {let resolve!: (value: T) => void, reject!: (error: unknown) => void; const promise = new Promise<T>((yes, no) => {resolve = yes; reject = no;}); return {promise, resolve, reject};}
async function ready(files = listing(), paths: SavedPath[] = [], props: Partial<React.ComponentProps<typeof FilesPage>> = {}) {
  mockBackend(files, paths); render(<FilesPage hosts={hosts} runtime={runtime} {...props}/>); return await screen.findByRole('table', {name: 'Remote files'});
}
async function switchToBeta() {
  fireEvent.click(screen.getByRole('button', {name: 'Switch file host'}));
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', {name: /beta/}));
  await screen.findByRole('heading', {name: 'beta'});
  await waitFor(() => expect(location.hash).not.toContain('files-switcher'));
}
const uploadCalls = () => apiMock.mock.calls.filter(([path]) => path.endsWith('/upload'));
beforeEach(() => {history.replaceState(null, '', '#/files'); apiMock.mockReset(); downloadMock.mockReset();});
afterEach(() => {vi.useRealTimers(); vi.unstubAllGlobals();});

describe('Files page', () => {
  it('opens a linked directory and navigates another link on the same host', async () => {
    mockBackend();
    const view = render(<FilesPage hosts={hosts} runtime={runtime} initialHostId={hosts[0].id} initialPath="/linked #&?中"/>);
    await screen.findByRole('table', {name: 'Remote files'});
    const request = apiMock.mock.calls.find(([path]) => path.includes('/files'))![0];
    expect(new URL(`http://local${request}`).searchParams.get('path')).toBe('/linked #&?中');
    expect(apiMock.mock.calls.filter(([path]) => path.includes('/files'))).toHaveLength(1);
    view.rerender(<FilesPage hosts={hosts} runtime={runtime} initialHostId={hosts[0].id} initialPath="/another folder"/>);
    await waitFor(() => expect(apiMock.mock.calls.filter(([path]) => path.includes('/files'))).toHaveLength(2));
    const next = apiMock.mock.calls.filter(([path]) => path.includes('/files')).at(-1)![0];
    expect(new URL(`http://local${next}`).searchParams.get('path')).toBe('/another folder');
    expect(screen.getAllByRole('tab')).toHaveLength(1);
  });

  it('debounces server filtering for 300 ms and encodes the host, directory and query', async () => {
    vi.useFakeTimers(); mockBackend(); render(<FilesPage hosts={hosts} runtime={runtime}/>);
    await act(async () => {});
    expect(apiMock).toHaveBeenCalledWith('/hosts/a%2F%23/files', expect.objectContaining({signal: expect.any(AbortSignal)}));
    fireEvent.click(screen.getByRole('button', {name: 'Edit remote path'}));
    fireEvent.change(screen.getByRole('textbox', {name: 'Remote directory path'}), {target: {value: '/space dir/#&?中'}});
    fireEvent.click(screen.getByRole('button', {name: 'Go'})); await act(async () => {});
    const navigation = apiMock.mock.calls.filter(([path]) => path.includes('/files')).at(-1)![0];
    expect(new URL(`http://local${navigation}`).searchParams.get('path')).toBe('/space dir/#&?中');
    const before = apiMock.mock.calls.filter(([path]) => path.includes('/files')).length;
    fireEvent.change(screen.getByRole('textbox', {name: 'Filter this folder'}), {target: {value: 'file #&?中'}});
    await act(async () => {vi.advanceTimersByTime(299);});
    expect(apiMock.mock.calls.filter(([path]) => path.includes('/files'))).toHaveLength(before);
    await act(async () => {vi.advanceTimersByTime(1);});
    const search = apiMock.mock.calls.filter(([path]) => path.includes('/files')).at(-1)![0];
    expect(new URL(`http://local${search}`).searchParams.get('name_filter')).toBe('file #&?中');
    expect(new URL(`http://local${search}`).searchParams.get('path')).toBe('/space dir/#&?中');
  });

  it('ignores an old host response even when the transport ignores abort', async () => {
    const old = deferred<DirectoryListing>();
    apiMock.mockImplementation(async path => path.endsWith('/paths') ? [] : path.startsWith('/hosts/b/') ? listing({alias: 'beta', entries: [entry('beta-file')]}) : old.promise);
    render(<FilesPage hosts={hosts} runtime={runtime}/>); await switchToBeta();
    expect(await screen.findByRole('button', {name: 'beta-file'})).toBeVisible();
    await act(async () => {old.resolve(listing({entries: [entry('stale-alpha-file')]}));});
    expect(screen.queryByRole('button', {name: 'stale-alpha-file'})).not.toBeInTheDocument();
    expect(screen.getByRole('button', {name: 'beta-file'})).toBeVisible();
  });

  it('keeps separate tab folders and closes a browser tab without terminating SSH', async () => {
    await ready(); fireEvent.click(screen.getByRole('button', {name: 'New file tab'}));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', {name: /beta/}));
    await screen.findByRole('heading', {name: 'beta'});
    expect(screen.getAllByRole('tab')).toHaveLength(2);
    fireEvent.click(screen.getByRole('tab', {name: /alpha/}));
    await screen.findByRole('heading', {name: 'alpha'});
    expect(screen.getByRole('tab', {name: /alpha/})).toHaveAttribute('aria-selected', 'true');
    fireEvent.keyDown(screen.getByRole('tab', {name: /alpha/}), {key: 'ArrowRight'});
    await screen.findByRole('heading', {name: 'beta'});
    expect(screen.getByRole('tab', {name: /beta/})).toHaveFocus();
    fireEvent.keyDown(screen.getByRole('tab', {name: /beta/}), {key: 'Home'});
    await screen.findByRole('heading', {name: 'alpha'});
    expect(screen.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', screen.getByRole('tab', {name: /alpha/}).id);
    fireEvent.click(screen.getByRole('button', {name: 'Close beta tab'}));
    expect(screen.getAllByRole('tab')).toHaveLength(1);
    expect(apiMock.mock.calls.every(([path, options]) => path.includes('/files') || path.endsWith('/paths') && !options?.method)).toBe(true);
  });

  it('shows returned, matched and parsed counts and explains truncated and unparsed results', async () => {
    await ready(listing({entries: [entry('visible'), entry('.hidden', {hidden: true})], total: 8000, matching_total: 5200, truncated: true, unparsed: 2}));
    expect(screen.getByText(/Showing 1 of 2 returned · 5200 matches · 8000 parsed entries/)).toBeVisible();
    expect(screen.getByText(/Results are truncated/)).toBeVisible();
    expect(screen.getByText(/2 entries could not be parsed/)).toBeVisible();
    expect(screen.queryByRole('button', {name: '.hidden'})).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', {name: 'Hidden files (1)'}));
    expect(screen.getByRole('button', {name: '.hidden'})).toBeVisible();
    expect(screen.getByText(/Showing 2 of 2 returned/)).toBeVisible();
  });

  it('uses metadata fallbacks without inventing exact timestamps or match counts', async () => {
    const table = await ready(listing({matching_total: null, entries: [entry('metadata', {owner: '', group: '', modified: 'Oct 14 12:00', modified_at: null, permissions: '', uid: 42, gid: 43})]}));
    expect(within(table).getByText('42')).toBeVisible();
    expect(within(table).getByText('Oct 14 12:00')).toHaveAttribute('title', 'Exact timestamp unavailable');
    expect(screen.getByText(/match count unavailable/)).toBeVisible();
    fireEvent.click(screen.getByRole('button', {name: 'metadata'}));
    expect(screen.getByText('/srv/app/metadata')).toBeVisible();
    expect(screen.getByText('43')).toBeVisible();
  });

  it('sorts known exact modification dates and keeps unknown dates last', async () => {
    const table = await ready(listing({entries: [entry('unknown'), entry('new', {modified_at: '2026-10-15T00:00:00Z'}), entry('old', {modified_at: '2025-01-01T00:00:00Z'})]}));
    fireEvent.click(within(table).getByRole('button', {name: 'modified'}));
    let rows = within(table).getAllByRole('row').slice(2);
    expect(rows.map(row => within(row).getByRole('button', {name: /^(unknown|new|old)$/}).textContent)).toEqual(['old', 'new', 'unknown']);
    fireEvent.click(within(table).getByRole('button', {name: 'modified ↑'}));
    rows = within(table).getAllByRole('row').slice(2);
    expect(rows.map(row => within(row).getByRole('button', {name: /^(unknown|new|old)$/}).textContent)).toEqual(['new', 'old', 'unknown']);
  });

  it('renders large directories in batches and keeps the complete returned count', async () => {
    await ready(listing({entries: Array.from({length: 600}, (_, index) => entry(`file-${index.toString().padStart(3, '0')}`)), total: 600, matching_total: 600}));
    expect(screen.getByText(/Showing 250 of 600 returned/)).toBeVisible();
    expect(screen.queryByRole('button', {name: 'file-499'})).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', {name: 'Show next 250 entries'}));
    expect(screen.getByRole('button', {name: 'file-499'})).toBeVisible();
    expect(screen.getByText(/Showing 500 of 600 returned/)).toBeVisible();
  }, 10_000);

  it('does not retry a failed listing until the user requests it', async () => {
    apiMock.mockImplementation(async path => {if (path.endsWith('/paths')) return []; throw new ApiError(502, 'auth_failed', 'Authentication failed');});
    render(<FilesPage hosts={hosts} runtime={runtime}/>);
    expect(await screen.findByRole('alert')).toHaveTextContent('Authentication failed');
    expect(apiMock.mock.calls.filter(([path]) => path.includes('/files'))).toHaveLength(1);
    mockBackend(); fireEvent.click(screen.getByRole('button', {name: 'Retry folder'}));
    expect(await screen.findByRole('table', {name: 'Remote files'})).toBeVisible();
    expect(apiMock.mock.calls.filter(([path]) => path.includes('/files'))).toHaveLength(2);
  });

  it('keeps conflict retries bound to the original file, host and path after switching hosts', async () => {
    await ready(); const pending = deferred<unknown>();
    apiMock.mockImplementation(async (path, options) => path.endsWith('/upload') ? pending.promise : path.endsWith('/paths') ? [] : listing({alias: path.startsWith('/hosts/b/') ? 'beta' : 'alpha'}));
    const file = new File(['data'], 'report #&?.txt');
    fireEvent.change(screen.getByLabelText('Upload file'), {target: {files: [file]}});
    expect(uploadCalls()).toHaveLength(1);
    const original = uploadCalls()[0][1]!.body as FormData;
    expect(original.get('overwrite')).toBe('false'); expect(original.get('path')).toBe('/srv/app/report #&?.txt');
    await switchToBeta();
    await act(async () => {pending.reject(new ApiError(409, 'exists', 'Already exists', {type: 'file', size: 9}));});
    const dialog = await screen.findByRole('dialog', {name: 'File already exists'});
    expect(within(dialog).getByText('alpha')).toBeVisible();
    expect(uploadCalls()).toHaveLength(1);
    apiMock.mockImplementation(async path => path.endsWith('/upload') ? {bytes: 4} : path.endsWith('/paths') ? [] : listing());
    const betaLists = apiMock.mock.calls.filter(([path]) => path.startsWith('/hosts/b/files')).length;
    fireEvent.click(within(dialog).getByRole('button', {name: 'Overwrite this file'}));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    const [retryPath, retryOptions] = uploadCalls()[1]; const retry = retryOptions!.body as FormData;
    expect(retryPath).toBe('/hosts/a%2F%23/upload'); expect(retry.get('file')).toBe(file);
    expect(retry.get('path')).toBe('/srv/app/report #&?.txt'); expect(retry.get('overwrite')).toBe('true');
    expect(apiMock.mock.calls.filter(([path]) => path.startsWith('/hosts/b/files'))).toHaveLength(betaLists);
  });

  it('captures the upload destination before opening the native file chooser', async () => {
    await ready();
    fireEvent.click(screen.getByRole('button', {name: 'Upload here'}));
    await switchToBeta();
    const file = new File(['data'], 'picked.txt');
    fireEvent.change(screen.getByLabelText('Upload file'), {target: {files: [file]}});
    await waitFor(() => expect(uploadCalls()).toHaveLength(1));
    const [path, options] = uploadCalls()[0];
    expect(path).toBe('/hosts/a%2F%23/upload');
    expect((options!.body as FormData).get('path')).toBe('/srv/app/picked.txt');
  });

  it('offers cancel and safe rename after a conflict, without automatic overwrites', async () => {
    await ready();
    apiMock.mockImplementation(async path => {if (path.endsWith('/upload')) throw new ApiError(409, 'exists', 'Already exists'); return path.endsWith('/paths') ? [] : listing();});
    const file = new File(['data'], 'report.txt'); fireEvent.change(screen.getByLabelText('Upload file'), {target: {files: [file]}});
    let dialog = await screen.findByRole('dialog', {name: 'File already exists'});
    fireEvent.change(within(dialog).getByRole('textbox', {name: 'New filename'}), {target: {value: '../escape'}});
    expect(within(dialog).getByRole('button', {name: 'Upload with new name'})).toBeDisabled();
    fireEvent.click(within(dialog).getByRole('button', {name: 'Cancel'})); expect(uploadCalls()).toHaveLength(1);
    await waitFor(() => expect(location.hash).not.toContain('files-conflict'));
    fireEvent.change(screen.getByLabelText('Upload file'), {target: {files: [file]}});
    dialog = await screen.findByRole('dialog', {name: 'File already exists'});
    fireEvent.change(within(dialog).getByRole('textbox', {name: 'New filename'}), {target: {value: 'report (2).txt'}});
    apiMock.mockImplementation(async path => path.endsWith('/upload') ? {bytes: 4} : path.endsWith('/paths') ? [] : listing());
    fireEvent.click(within(dialog).getByRole('button', {name: 'Upload with new name'}));
    await waitFor(() => expect(uploadCalls()).toHaveLength(3));
    const form = uploadCalls()[2][1]!.body as FormData;
    expect(form.get('path')).toBe('/srv/app/report (2).txt'); expect(form.get('file')).toBe(file); expect(form.get('overwrite')).toBe('false');
  });

  it('uses the runtime byte limit, permits zero-byte files, and blocks symlink and special downloads', async () => {
    const localRuntime = {...runtime, capabilities: {...runtime.capabilities, max_web_transfer_bytes: 8}};
    await ready(listing({entries: [entry('zero', {size: 0}), entry('at-limit', {size: 8}), entry('large', {size: 9}), entry('link', {type: 'symlink', link_target: '/secret'}), entry('device', {type: 'special'})]}), [], {runtime: localRuntime});
    expect(screen.getByRole('button', {name: 'Download zero'})).toBeEnabled();
    expect(screen.getByRole('button', {name: 'Download at-limit'})).toBeEnabled();
    ['large', 'link', 'device'].forEach(name => expect(screen.getByRole('button', {name: `Download ${name}`})).toBeDisabled());
    fireEvent.change(screen.getByLabelText('Upload file'), {target: {files: [new File(['123456789'], 'large-upload')]}});
    expect(screen.getByRole('alert')).toHaveTextContent('Web uploads are limited to 8 B'); expect(uploadCalls()).toHaveLength(0);
    const createObjectURL = vi.fn(() => 'blob:download'), revokeObjectURL = vi.fn();
    vi.stubGlobal('URL', Object.assign(URL, {createObjectURL, revokeObjectURL}));
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    downloadMock.mockResolvedValue({blob: new Blob([]), filename: 'zero'});
    fireEvent.click(screen.getByRole('button', {name: 'Download zero'}));
    await waitFor(() => expect(downloadMock).toHaveBeenCalledWith('/hosts/a%2F%23/download', {path: '/srv/app/zero'}));
    expect(click).toHaveBeenCalledOnce(); await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith('blob:download'));
    fireEvent.change(screen.getByLabelText('Upload file'), {target: {files: [new File(['12345678'], 'exact-upload')]}});
    await waitFor(() => expect(uploadCalls()).toHaveLength(1));
  });

  it.each([[502, 'auth_failed', 'Authentication failed'], [403, 'csrf_failed', 'CSRF verification failed'], [401, 'unauthorized', 'Session expired'], [501, 'unsupported', 'Safe upload is unsupported']] as const)('does not replay an upload after %s %s', async (status, code, message) => {
    await ready(); apiMock.mockImplementation(async path => {if (path.endsWith('/upload')) throw new ApiError(status, code, message); return path.endsWith('/paths') ? [] : listing();});
    fireEvent.change(screen.getByLabelText('Upload file'), {target: {files: [new File(['data'], 'upload.txt')]}});
    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    expect(uploadCalls()).toHaveLength(1); expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('creates, visits, edits and deletes saved paths through the real API contract', async () => {
    const first = saved(); await ready(listing(), [first]);
    apiMock.mockImplementation(async (path, options) => {
      if (path.includes('/files')) return listing({path: first.path});
      if (path.endsWith('/used')) return {...first, last_used_at: '2026-10-15T12:00:00Z'};
      if (options?.method === 'POST') return saved({id: 'new', label: 'Release', path: '/srv/release'});
      if (options?.method === 'PATCH') return {...first, label: 'Renamed logs'};
      if (options?.method === 'DELETE') return [];
      return [first];
    });
    fireEvent.click((await screen.findByText(first.label)).closest('button')!);
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('/hosts/a%2F%23/paths/saved%2F%23/used', {method: 'POST'}));
    const visited = apiMock.mock.calls.filter(([path]) => path.includes('/files')).at(-1)![0];
    expect(new URL(`http://local${visited}`).searchParams.get('path')).toBe(first.path);
    await screen.findByRole('table', {name: 'Remote files'});
    fireEvent.click(screen.getByRole('button', {name: '☆ Save path'})); let dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByRole('textbox', {name: 'Label'}), {target: {value: 'Release'}});
    fireEvent.change(within(dialog).getByRole('textbox', {name: 'Remote path'}), {target: {value: '/srv/release'}});
    fireEvent.click(within(dialog).getByRole('button', {name: 'Save path'}));
    await waitFor(() => {
      expect(location.hash).not.toContain('files-path');
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
    expect(apiMock).toHaveBeenCalledWith('/hosts/a%2F%23/paths', expect.objectContaining({method: 'POST', body: JSON.stringify({label: 'Release', path: '/srv/release', notes: ''})}));
    fireEvent.click(screen.getByRole('button', {name: 'Edit saved path App logs'})); dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByRole('textbox', {name: 'Label'}), {target: {value: 'Renamed logs'}});
    fireEvent.click(within(dialog).getByRole('button', {name: 'Save path'}));
    await screen.findByRole('button', {name: 'Delete saved path Renamed logs'});
    // The updated sidebar precedes the asynchronous dialog-history close.
    await waitFor(() => {
      expect(location.hash).not.toContain('files-path');
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
    expect(apiMock).toHaveBeenCalledWith('/hosts/a%2F%23/paths/saved%2F%23', expect.objectContaining({method: 'PATCH'}));
    fireEvent.click(screen.getByRole('button', {name: 'Delete saved path Renamed logs'}));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', {name: 'Delete saved path'}));
    await waitFor(() => {
      expect(screen.queryByRole('button', {name: 'Delete saved path Renamed logs'})).not.toBeInTheDocument();
      expect(location.hash).not.toContain('files-delete-path');
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
    expect(apiMock).toHaveBeenCalledWith('/hosts/a%2F%23/paths/saved%2F%23', {method: 'DELETE'});
  });
});
