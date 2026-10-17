import {Fragment, useEffect, useMemo, useRef, useState, type ReactNode} from 'react';
import {api, json, segment} from '../api';
import {useDialogState, usePreference, useResource} from '../hooks';
import type {Credential, Diagnosis, Host, HostRoute, ImportPreview, RuntimeInfo, SavedPath, Skill, TestResult, Workspace} from '../types';
import {CredentialEditor} from './CredentialsPage';
import {Badge, Button, EmptyState, ErrorNotice, Field, Icon, KeyHint, Modal, PageHeader, formatDate, relativeTime} from '../ui';

type Props = {hosts: Host[]; credentials: Credential[]; runtime: RuntimeInfo; onRefresh: () => Promise<void>; hash: string; onCreateCredential: (done: (credential: Credential) => void) => void};
export function statusLabel(host: Host) {return host.last_test_status === 'ok' ? `OK${host.last_test_latency_ms == null ? '' : ` · ${host.last_test_latency_ms} ms`}` : host.last_test_status === 'failed' ? host.last_test_error_code?.replaceAll('-', ' ') || 'Failed' : 'Never checked';}
export function visibleHostText(host: Host, details: boolean) {return [host.alias, ...host.tags, ...(details ? [host.hostname, host.username, host.notes, host.credential_label || ''] : [])].join(' ').toLocaleLowerCase();}
const systemMarks: Record<string,string> = {linux:'LX',ubuntu:'Ub',debian:'Db',fedora:'Fd',centos:'Ce',almalinux:'Al',rocky:'Rk',arch:'Ar',macos:'Mc',darwin:'Mc',windows:'Wi',freebsd:'FB'};
function hostHash(host: Host) {return `#/hosts/${segment(host.alias)}`;}

export default function HostsPage({hosts, credentials, runtime, onRefresh, hash, onCreateCredential}: Props) {
  const [query, setQuery] = useState(''), [status, setStatus] = useState('any'), [tag, setTag] = useState(''), [group, setGroup] = usePreference('host-group', 'route'), [details, setDetails] = usePreference('host-details', true);
  const [selection, setSelection] = useState<Set<string>>(new Set()), [editing, setEditing] = useDialogState<Host | 'new' | null>('host-edit',null), [duplicate, setDuplicate] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(), [notice, setNotice] = useState('');
  const [confirmDelete, setConfirmDelete] = useDialogState<Host | null>('host-delete',null), [diagnosis, setDiagnosis] = useDialogState<Diagnosis | null>('host-diagnosis',null), [results, setResults] = useDialogState<TestResult[] | null>('host-results',null), [importing, setImporting] = useDialogState('host-import',false), [tagging, setTagging] = useDialogState<Host[] | null>('host-tags',null);
  const [observationResults, setObservationResults] = useDialogState<TestResult[] | null>('host-observation-results',null), [bulkAction, setBulkAction] = useState<'test' | 'observe' | null>(null);
  const [pins, setPins] = usePreference<string[]>('host-pins', ['files', 'check', 'copy']);
  const [cursorId, setCursorId] = useState<string | null>(null);
  const [preview, setPreview] = useState<{host: Host; left: number; top: number}>();
  const previewTimer = useRef<number | undefined>(undefined), hideTimer = useRef<number | undefined>(undefined);
  const clearPreviewTimers = () => {if (previewTimer.current !== undefined) {window.clearTimeout(previewTimer.current); previewTimer.current = undefined;} if (hideTimer.current !== undefined) {window.clearTimeout(hideTimer.current); hideTimer.current = undefined;}};
  const hidePreview = () => {clearPreviewTimers(); setPreview(undefined);};
  const scheduleHide = () => {clearPreviewTimers(); hideTimer.current = window.setTimeout(() => {hideTimer.current = undefined; setPreview(undefined);}, 140);};
  const showPreview = (host: Host, anchor: HTMLElement, immediate = false) => {
    if (!details) return;
    if (typeof window.matchMedia === 'function' && !window.matchMedia('(hover: hover)').matches) return;
    clearPreviewTimers();
    const display = () => {
      const rect = anchor.getBoundingClientRect(), width = Math.min(360, window.innerWidth - 32), height = 560;
      const left = Math.max(16, Math.min(rect.left + 16, window.innerWidth - width - 16));
      const below = rect.bottom + 8, above = rect.top - height - 8, maxTop = Math.max(16, window.innerHeight - height - 16);
      setPreview({host, left, top: below + height <= window.innerHeight - 16 ? below : above >= 16 ? above : maxTop});
      previewTimer.current = undefined;
    };
    if (immediate) display(); else previewTimer.current = window.setTimeout(display, 260);
  };
  const routeAlias = (() => {try {return decodeURIComponent(hash.split('?')[0].split('/')[2] || '');} catch {return '';}})();
  const selectedHost = hosts.find(host => host.alias === routeAlias || host.id === routeAlias);
  const legacyCredential = hash.startsWith('#/servers/') && hash.split('?')[0].endsWith('/credential');
  useEffect(() => {setSelection(previous => new Set([...previous].filter(id => hosts.some(host => host.id === id))));}, [hosts]);
  const blocked = useMemo(() => new Map(hosts.filter(host => host.proxy_jumps.length).map(host => {
    const down = host.proxy_jumps.find(jump => hosts.some(candidate => candidate.alias === jump && candidate.last_test_status === 'failed'));
    return [host.id, down] as const;
  }).filter(([, down]) => down)), [hosts]);
  const filtered = hosts.filter(host => visibleHostText(host, details).includes(query.toLocaleLowerCase()) && (!tag || (tag === '\u0000untagged' ? !host.tags.length : host.tags.includes(tag))) && (status === 'any' || (status === 'never' ? !host.last_test_at : host.last_test_status === status)));
  const scopes = useMemo(() => {
    const counts = new Map<string, number>();
    hosts.forEach(host => host.tags.forEach(name => counts.set(name, (counts.get(name) || 0) + 1)));
    return [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([name, count]) => ({name, count}));
  }, [hosts]);
  const untagged = hosts.filter(host => !host.tags.length).length;
  const groups = useMemo(() => {
    const map = new Map<string, Host[]>();
    filtered.forEach(host => {const label = group === 'tag' ? host.tags[0] || 'Untagged' : group === 'route' ? host.proxy_jumps.length ? `Via ${host.proxy_jumps.join(' → ')}` : 'Direct connections' : ''; map.set(label, [...(map.get(label) || []), host]);});
    return [...map];
  }, [filtered, group]);
  const rows = useMemo(() => groups.flatMap(([label, members]) => members.map(host => ({host, label}))), [groups]);
  const cursor = rows.findIndex(row => row.host.id === cursorId);
  const action = async (task: () => Promise<void>) => {if (busy) return; setBusy(true); setError(undefined); setNotice(''); try {await task();} catch (reason) {setError(reason);} finally {setBusy(false);}};
  const check = (host: Host) => action(async () => {const result = await api<TestResult>(`/hosts/${segment(host.id)}/test`, json('POST', {timeout: 8})); await onRefresh(); setNotice(`${host.alias}: ${result.message}`);});
  const observe = (host: Host) => {hidePreview(); return action(async () => {const result = await api<TestResult>(`/hosts/${segment(host.id)}/observe`, json('POST', {timeout: 8})); await onRefresh(); setNotice(`${host.alias}: ${result.message}`);});};
  const batch = (kind: 'test' | 'observe') => action(async () => {
    setBulkAction(kind);
    try {
      const response = await api<{results: TestResult[]}>(`/hosts/${kind}`, json('POST', {identifiers: [...selection], timeout: 8, concurrency: runtime.capabilities.max_check_concurrency}));
      if (kind === 'observe') setObservationResults(response.results); else setResults(response.results);
      await onRefresh();
    } finally {setBulkAction(null);}
  });
  const diagnose = (host: Host) => action(async () => {const result = await api<Diagnosis>(`/hosts/${segment(host.id)}/diagnose`, json('POST', {timeout: 8})); setDiagnosis(result); await onRefresh();});
  const copy = (host: Host) => action(async () => {await navigator.clipboard.writeText(`serverctl connect ${host.alias}`); setNotice(`Copied connection command for ${host.alias}`);});
  const files = (host: Host) => {location.hash = `#/files/${segment(host.id)}`;};
  const toggle = (id: string) => setSelection(previous => {const next = new Set(previous); next.has(id) ? next.delete(id) : next.add(id); return next;});
  const edit = (host: Host, copy = false) => {setDuplicate(copy); setEditing(host);};
  const pinned = (host: Host) => pins.filter(pin => ROW_ACTIONS.some(action => action.id === pin)).slice(0, 3);
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || event.metaKey || event.ctrlKey || event.altKey) return;
      if (document.querySelector('[role="dialog"]')) return;
      if (event.key === 'Escape') {setCursorId(null); hidePreview(); return;}
      const target = event.target;
      if (target instanceof HTMLElement && target.closest('button,a[href],input,textarea,select,summary,[contenteditable]:not([contenteditable="false"]),[role="button"],[role="link"]')) return;
      if (event.key === '/') {event.preventDefault(); document.querySelector<HTMLInputElement>('input[aria-label="Search hosts"]')?.focus(); return;}
      if (!rows.length) return;
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {event.preventDefault(); const next = Math.min(rows.length - 1, Math.max(0, cursor < 0 ? (event.key === 'ArrowDown' ? 0 : rows.length - 1) : cursor + (event.key === 'ArrowDown' ? 1 : -1))); setCursorId(rows[next].host.id); return;}
      const host = rows[cursor]?.host;
      if (!host) return;
      if (event.key === 'Enter') {event.preventDefault(); location.hash = hostHash(host);}
      else if (event.key.toLowerCase() === 'b') {event.preventDefault(); files(host);}
      else if (event.key.toLowerCase() === 't') {event.preventDefault(); void check(host);}
      else if (event.key.toLowerCase() === 'c') {event.preventDefault(); void copy(host);}
    };
    document.addEventListener('keydown', key);
    return () => document.removeEventListener('keydown', key);
  });
  useEffect(() => {if (selectedHost) hidePreview();}, [selectedHost]);
  useEffect(() => {if (!details) hidePreview();}, [details]);
  useEffect(() => {const hide = () => hidePreview(); document.addEventListener('scroll', hide, true); window.addEventListener('resize', hide); return () => {document.removeEventListener('scroll', hide, true); window.removeEventListener('resize', hide);};}, []);
  useEffect(() => () => clearPreviewTimers(), []);
  const summary = `${hosts.length} hosts · ${hosts.filter(host => host.last_test_status === 'ok' && !blocked.has(host.id)).length} reachable · ${hosts.filter(host => host.last_test_status === 'failed' && !blocked.has(host.id)).length} failing${blocked.size ? ` · ${blocked.size} blocked by a jump host` : ''} · ${hosts.filter(host => !host.last_test_at && !blocked.has(host.id)).length} never checked`;
  const focusRow = (id: string) => setCursorId(id);
  return <div className="page hosts-page">
    <PageHeader title="Hosts" description={summary}>
      <Button onClick={() => setDetails(!details)} className={details ? '' : 'pressed'}>{details ? 'Hide details' : 'Details hidden'}<KeyHint>⇧H</KeyHint></Button>
      <Button onClick={() => setImporting(true)}>Import SSH config</Button>
      <Button variant="primary" onClick={() => {setDuplicate(false); setEditing('new');}}><Icon name="plus"/>Add host</Button>
    </PageHeader>
    {!details && <div className="privacy-notice" role="status"><Icon name="lock"/><p>Addresses, users, locations, credential names and notes are masked, and copying them is disabled. Search only matches what is visible.</p><Button onClick={() => setDetails(true)}>Show details<KeyHint>⇧H</KeyHint></Button></div>}
    <ErrorNotice error={error}/>{notice && <div role="status" className="notice">{notice}</div>}
    <div className="hosts-toolbar">
      <label className="search-box search-field"><Icon name="search"/><input aria-label="Search hosts" placeholder={details ? 'Search alias, hostname, user, notes' : 'Search alias and tags'} value={query} onChange={event => setQuery(event.target.value)}/><KeyHint>/</KeyHint></label>
      <div className="scope-chips" role="group" aria-label="Filter by tag">
        <button className={`scope-chip${tag === '' ? ' selected' : ''}`} aria-pressed={tag === ''} onClick={() => setTag('')}>All <span className="count">{hosts.length}</span></button>
        {scopes.map(scope => <button key={scope.name} className={`scope-chip${tag === scope.name ? ' selected' : ''}`} aria-pressed={tag === scope.name} onClick={() => setTag(scope.name)}><span className={`tag-dot ${tagTone(scope.name)}`}/>{scope.name} <span className="count">{scope.count}</span></button>)}
        {!!untagged && <button className={`scope-chip${tag === '\u0000untagged' ? ' selected' : ''}`} aria-pressed={tag === '\u0000untagged'} onClick={() => setTag('\u0000untagged')}>Untagged <span className="count">{untagged}</span></button>}
      </div>
      <label className="status-filter">Status<select aria-label="Filter by status" value={status} onChange={event => setStatus(event.target.value)}><option value="any">Any</option><option value="ok">Reachable</option><option value="failed">Failing</option><option value="never">Never checked</option></select></label>
      <div className="group-filter"><span className="muted">Group</span><div className="segmented" aria-label="Group hosts">{['none','route','tag'].map(value => <button key={value} aria-pressed={group === value} onClick={() => setGroup(value)}>{value === 'route' && <Icon name="arrow" size={12}/>}{value[0].toUpperCase() + value.slice(1)}</button>)}</div></div>
    </div>
    {!!selection.size && <div className="toolbar selection-bar"><strong>{selection.size} selected</strong><Button disabled={busy || selection.size > runtime.capabilities.max_bulk_hosts} onClick={() => void batch('test')}>{bulkAction === 'test' ? 'Checking…' : 'Run checks'}</Button><Button disabled={busy || selection.size > runtime.capabilities.max_bulk_hosts} onClick={() => void batch('observe')}>{bulkAction === 'observe' ? 'Collecting…' : 'Collect host info'}</Button><Button onClick={() => setTagging(hosts.filter(host => selection.has(host.id)))}>Edit tags</Button><Button variant="ghost" onClick={() => setSelection(new Set())}>Clear selection</Button>{selection.size > runtime.capabilities.max_bulk_hosts && <span>Select at most {runtime.capabilities.max_bulk_hosts} hosts.</span>}</div>}
    {!filtered.length ? <EmptyState title={hosts.length ? 'No matching hosts' : 'Your servers, in one place'}><p>{hosts.length ? 'Try another search or filter.' : 'Add your first host or import your SSH configuration to get started.'}</p></EmptyState> : <div className="hosts-card">
      <div className="host-table-region">
        <div className="lane head" role="row">
          <div className="cell-check"><input type="checkbox" aria-label="Select all visible hosts" checked={filtered.length > 0 && filtered.every(host => selection.has(host.id))} onChange={event => setSelection(event.target.checked ? new Set(filtered.map(host => host.id)) : new Set())}/></div>
          <div className="cell-host" role="columnheader">Host<Icon name="up" size={9}/></div>
          <div className="cell-status" role="columnheader">Last check</div>
          {details && <div className="cell-endpoint" role="columnheader">Endpoint</div>}
          <div className="cell-tags" role="columnheader">Tags</div>
          {details && <div className="cell-credential" role="columnheader">Credential</div>}
          <div className="cell-actions" aria-label="Actions"/>
        </div>
        {rows.map(({host, label}, index) => {
          const down = blocked.get(host.id), isCursor = index === cursor;
          return <Fragment key={host.id}>{group !== 'none' && label && (index === 0 || rows[index - 1].label !== label) && <div className={`lane group-row${index === 0 ? ' first' : ''}`}><span>{label}</span><span className="muted">· {rows.filter(row => row.label === label).length}</span></div>}
            <div className={`lane host-row${isCursor ? ' cursor' : ''}${down ? ' blocked' : ''}${routeFlags(rows, index)}`} role="row" data-host={host.alias} onMouseDown={() => focusRow(host.id)} onMouseEnter={event => showPreview(host, event.currentTarget)} onMouseLeave={scheduleHide}>
              <div className="cell-check"><input type="checkbox" aria-label={`Select ${host.alias}`} checked={selection.has(host.id)} onChange={() => toggle(host.id)}/></div>
              <div className="cell-host">
                <OsTile logo={host.facts?.os_logo}/>
                <div className="host-lines">
                  <div className="host-name-line"><button className="host-name" aria-label={host.alias} aria-describedby={details && preview?.host.id === host.id ? 'host-preview' : undefined} onFocus={event => showPreview(host, event.currentTarget, true)} onBlur={hidePreview} onClick={() => {hidePreview(); location.hash = hostHash(host);}}>{host.alias}</button>{host.jump_for_count > 0 && <span className="jump-chip"><Icon name="arrow" size={10}/>Jump for {host.jump_for_count}</span>}</div>
                  <small>{details ? host.facts?.os_pretty || <><span className="host-os-unknown">OS unknown · </span><button className="link-button" aria-label={`Collect host info ${host.alias}`} title="OS unknown. Collect host info" onClick={() => void observe(host)} disabled={busy}>Collect host info</button></> : `${host.facts?.os_pretty || 'OS unknown'} · ••••••••`}</small>
                </div>
              </div>
              <div className="cell-status"><StatusCell host={host} reason={down}/></div>
              {details && <div className="cell-endpoint"><EndpointCell host={host}/></div>}
              <div className="cell-tags"><TagCell tags={host.tags} onAdd={() => setTagging([host])}/></div>
              {details && <div className="cell-credential"><span className="credential-name">{host.credential_label || 'Default SSH identity'}</span><small>{credentialKind(host.credential_kind)}</small></div>}
              <RowActions host={host} pins={pinned(host)} busy={busy} onFiles={() => files(host)} onCheck={() => void check(host)} onCopy={() => void copy(host)} onDiagnose={() => void diagnose(host)} onEdit={() => edit(host)} onDuplicate={() => edit(host, true)} onTags={() => setTagging([host])} onRemove={() => setConfirmDelete(host)} onPin={pin => setPins(pins.includes(pin) ? pins.filter(value => value !== pin) : [...pins, pin])}/>
            </div>
          </Fragment>;
        })}
      </div>
    </div>}
    {!!filtered.length && <div className="hosts-footer"><span>Showing {filtered.length === hosts.length ? `all ${hosts.length}` : `${filtered.length} of ${hosts.length}`} hosts. Select rows to edit tags, run checks or collect host info in bulk.</span><span className="footer-hints"><span><KeyHint>↑↓</KeyHint>move</span><span><KeyHint>↵</KeyHint>open details</span></span></div>}
    {details && preview && filtered.some(host => host.id === preview.host.id) && <HostPreview host={preview.host} left={preview.left} top={preview.top}/>}
    {selectedHost && !legacyCredential && <HostDetails key={selectedHost.id} host={selectedHost} hosts={hosts} credentials={credentials} onClose={() => {location.hash = '#/hosts';}} onRefresh={onRefresh} onEdit={() => edit(selectedHost)} onCheck={() => void check(selectedHost)} onObserve={() => void observe(selectedHost)} onDiagnose={() => void diagnose(selectedHost)} onFiles={() => files(selectedHost)} onCopy={() => void copy(selectedHost)} onTags={() => setTagging([selectedHost])} busy={busy}/>}
    {legacyCredential && selectedHost && <LegacyCredential key={selectedHost.id} host={selectedHost} credential={credentials.find(c=>c.id===selectedHost.credential_id)} runtime={runtime} onRefresh={onRefresh}/>}
    {editing && <HostEditor host={editing === 'new' ? undefined : editing} duplicate={duplicate} credentials={credentials} hosts={hosts} onCreateCredential={onCreateCredential} onClose={() => {setEditing(null); if (legacyCredential) location.hash = '#/hosts';}} onSaved={async () => {await onRefresh(); setEditing(null); if (legacyCredential) location.hash = '#/hosts';}}/>}
    {confirmDelete && <Modal title={`Remove ${confirmDelete.alias}?`} onClose={() => setConfirmDelete(null)}><p>This removes the saved host and its bindings. The remote server is unchanged.</p><div className="modal-footer"><Button onClick={() => setConfirmDelete(null)}>Cancel</Button><Button variant="danger" disabled={busy} onClick={() => void action(async () => {await api(`/hosts/${segment(confirmDelete.id)}`, json('DELETE')); setConfirmDelete(null); location.hash = '#/hosts'; await onRefresh();})}>Remove host</Button></div></Modal>}
    {diagnosis && <Modal title={`Diagnosis · ${diagnosis.alias}`} onClose={() => setDiagnosis(null)}><div className="stack">{diagnosis.checks.map(check => <div key={check.id}><Badge tone={check.status}>{check.status}</Badge> <strong>{check.label}</strong><p className="muted">{check.message}</p></div>)}</div></Modal>}
    {results && <BatchResults title="Batch check results" results={results} onClose={() => setResults(null)}/>}
    {observationResults && <BatchResults title="Host info results" results={observationResults} onClose={() => setObservationResults(null)}/>}
    {importing && <ImportDialog onClose={() => setImporting(false)} onSaved={onRefresh}/>}
    {tagging && <BulkTags hosts={tagging} onClose={() => setTagging(null)} onSaved={onRefresh}/>}
  </div>;
}

function BatchResults({title, results, onClose}: {title: string; results: TestResult[]; onClose: () => void}) {
  return <Modal title={title} onClose={onClose}><div className="stack">{results.map(result => <div key={result.alias}><Badge tone={result.ok ? 'ok' : 'failed'}>{result.ok ? 'OK' : 'Failed'}</Badge> <strong>{result.alias}</strong><p className="muted">{result.message}</p></div>)}</div></Modal>;
}

type RowAction = {id: string; label: string; key?: string; icon?: string};
const ROW_ACTIONS: RowAction[] = [
  {id: 'files', label: 'Browse files', key: 'B', icon: 'folder'},
  {id: 'check', label: 'Run check', key: 'T', icon: 'check'},
  {id: 'copy', label: 'Copy SSH command', key: 'C', icon: 'copy'},
  {id: 'diagnose', label: 'Run full diagnosis', key: 'D', icon: 'check'},
  {id: 'edit', label: 'Edit host', key: 'E', icon: 'edit'},
  {id: 'tags', label: 'Edit tags', key: 'L', icon: 'tags'},
  {id: 'duplicate', label: 'Duplicate host', icon: 'copy'},
];

const OS_MARKS: Record<string, {color: string; path: ReactNode}> = {
  ubuntu: {color: '#E95420', path: <><circle cx="12" cy="12" r="7" fill="none" stroke="currentColor" strokeWidth="2"/><circle cx="12" cy="8.2" r="1.6" fill="currentColor"/><circle cx="8.7" cy="14" r="1.6" fill="currentColor"/><circle cx="15.3" cy="14" r="1.6" fill="currentColor"/></>},
  debian: {color: '#A81D33', path: <path d="M12 4a8 8 0 1 0 6.9 4M12 8a4 4 0 1 0 3.4 2" fill="none" stroke="currentColor" strokeWidth="2"/>},
  rocky: {color: '#10B981', path: <path d="M4 18l5-9 3 5 2-3 6 7z" fill="none" stroke="currentColor" strokeWidth="2"/>},
  alpine: {color: '#0D597F', path: <path d="M3 18l6-11 4 7 2-3 6 7z" fill="none" stroke="currentColor" strokeWidth="2"/>},
};

function OsTile({logo, size = 28}: {logo?: string | null; size?: number}) {
  const mark = logo ? OS_MARKS[logo] : undefined;
  const glyph = Math.round(size * 0.61);
  if (!mark) return <span className="os-tile unknown" style={{width: size, height: size}}><Icon name="hosts" size={glyph - 2}/></span>;
  return <span className="os-tile" style={{color: mark.color, width: size, height: size}}><svg width={glyph} height={glyph} viewBox="0 0 24 24" fill="none" aria-hidden="true">{mark.path}</svg></span>;
}

function StatusCell({host, reason}: {host: Host; reason?: string}) {
  if (reason) return <div className="status-cell"><span className="status-line"><span className="status-dot blocked-ring"/><span className="status-text muted">Blocked</span></span><small>{reason} is down</small></div>;
  if (!host.last_test_at) return <div className="status-cell"><span className="status-line"><span className="status-dot idle-ring"/><span className="status-text muted">Not checked yet</span></span><small>Check connectivity</small></div>;
  const ok = host.last_test_status === 'ok';
  return <div className="status-cell"><span className="status-line"><span className={`status-dot ${ok ? 'ok' : 'failed'}`}/><span className={`status-text ${ok ? 'ok' : 'failed'}`}>{ok ? `OK${host.last_test_latency_ms == null ? '' : ` · ${host.last_test_latency_ms} ms`}` : statusLabel(host)}</span></span><small>{relativeTime(host.last_test_at)}</small></div>;
}

function EndpointCell({host}: {host: Host}) {
  const publicAddress = host.address_scope === 'public';
  const flag = host.geo?.country_code ? [...host.geo.country_code.toUpperCase()].map(letter => String.fromCodePoint(127397 + letter.charCodeAt(0))).join('') : '';
  return <div className="endpoint-cell"><span className="address"><span className="address-icon">{publicAddress && flag ? flag : <Icon name={host.address_scope === 'lan' ? 'hosts' : publicAddress ? 'globe' : 'cloud'} size={12}/>}</span><span className="mono">{host.username}@{host.hostname}:{host.port}</span></span><small>{[host.geo ? [host.geo.city, host.geo.country_code].filter(Boolean).join(', ') : scopeLabel(host.address_scope), host.proxy_jumps.length ? `via ${host.proxy_jumps.join(', ')}` : 'direct'].join(' · ')}</small></div>;
}

function TagCell({tags, onAdd}: {tags: string[]; onAdd: () => void}) {
  if (!tags.length) return <button className="tag-chip empty" onClick={onAdd}>Add tag</button>;
  return <span className="tag-cell">{tags.slice(0, 2).map(name => <span className="tag-chip" key={name}><span className={`tag-dot ${tagTone(name)}`}/>{name}</span>)}{tags.length > 2 && <span className="tag-chip more">+{tags.length - 2}</span>}</span>;
}

function RowActions({host, pins, busy, onFiles, onCheck, onCopy, onDiagnose, onEdit, onDuplicate, onTags, onRemove, onPin}: {host: Host; pins: string[]; busy: boolean; onFiles: () => void; onCheck: () => void; onCopy: () => void; onDiagnose: () => void; onEdit: () => void; onDuplicate: () => void; onTags: () => void; onRemove: () => void; onPin: (pin: string) => void}) {
  const handler: Record<string, () => void> = {files: onFiles, check: onCheck, copy: onCopy, diagnose: onDiagnose, edit: onEdit, tags: onTags, duplicate: onDuplicate};
  return <div className="cell-actions row-actions">
    <div className="pinned-group">{pins.map(pin => <Button key={pin} variant="ghost" className="action-slot" disabled={busy} aria-label={`${ROW_ACTIONS.find(action => action.id === pin)?.label} ${host.alias}`} title={ROW_ACTIONS.find(action => action.id === pin)?.label} onClick={() => handler[pin]?.()}><Icon name={ROW_ACTIONS.find(action => action.id === pin)?.icon || 'file'} size={15}/></Button>)}
      <details className="row-menu"><summary aria-label={`More actions for ${host.alias}`}><Icon name="more" size={15}/></summary>
        <div className="row-menu-items">
          <div className="menu-section"><span className="menu-label">Pinned to row</span><span className="menu-count">{pins.length} of 3</span></div>
          {pins.map(pin => <button key={pin} className="menu-item" onClick={() => onPin(pin)}>{ROW_ACTIONS.find(action => action.id === pin)?.label}<KeyHint>{ROW_ACTIONS.find(action => action.id === pin)?.key}</KeyHint><span className="pin-mark filled"/></button>)}
          <div className="menu-divider"/>
          <div className="menu-section"><span className="menu-label">All actions</span></div>
          {ROW_ACTIONS.filter(action => action.id !== 'tags' && action.id !== 'duplicate').map(action => <button key={action.id} className="menu-item" onClick={action.id === 'diagnose' ? onDiagnose : action.id === 'edit' ? onEdit : () => onPin(action.id)}>{action.label}<KeyHint>{action.key}</KeyHint><span className={`pin-mark${pins.includes(action.id) ? ' filled' : ''}`}/></button>)}
          <button className="menu-item" onClick={onTags}>Edit tags<KeyHint>L</KeyHint><span className="pin-mark"/></button>
          <button className="menu-item" onClick={onDuplicate}>Duplicate host<span className="pin-mark"/></button>
          <div className="menu-divider"/>
          <button className="menu-item danger" onClick={onRemove}>Remove host…</button>
          <p className="menu-hint">Pin up to 3 actions. They show on every row; remove is never pinnable.</p>
        </div>
      </details>
    </div>
  </div>;
}

function toGiB(value: string, unit: string) {
  const number = Number(value);
  const scale = /^K/i.test(unit) ? 1 / 1024 / 1024 : /^M/i.test(unit) ? 1 / 1024 : /^G/i.test(unit) ? 1 : /^T/i.test(unit) ? 1024 : /^P/i.test(unit) ? 1024 * 1024 : NaN;
  return Number.isFinite(number) && Number.isFinite(scale) ? number * scale : undefined;
}
function parseMemory(value?: string | null): {label: string; percent: number; total: string} | undefined {
  const match = /Mem:\s*([\d.]+)\s*([KMGT]i?)\s+([\d.]+)\s*([KMGT]i?)/i.exec(value || '');
  if (!match) return undefined;
  const total = toGiB(match[1], match[2]), used = toGiB(match[3], match[4]);
  if (total === undefined || used === undefined || total <= 0) return undefined;
  const format = (number: number) => Number.isInteger(number) ? String(number) : number.toFixed(1);
  return {label: `${format(used)} / ${format(total)} GiB`, percent: Math.max(0, Math.min(100, Math.round(used / total * 100))), total: `${format(total)} GiB`};
}
function parseDisk(value?: string | null): {label: string; percent: number; total: string} | undefined {
  const match = /\s([\d.]+)\s*([KMGTP]i?B?)\s+([\d.]+)\s*([KMGTP]i?B?)\s+[\d.]+[KMGTP]i?B?\s+(\d{1,3})%\s+/.exec(value || '');
  if (!match) return undefined;
  const unit = `${match[2].replace(/B$/i, '')}B`;
  return {label: `${match[3]} / ${match[1]} ${unit}`, percent: Math.max(0, Math.min(100, Number(match[5]))), total: `${match[1]} ${unit}`};
}
function tagTone(name: string) {const TONES = ['rose', 'amber', 'blue', 'green'];let hash = 0;for (const character of name) hash = (hash * 31 + character.charCodeAt(0)) % 997;return TONES[hash % TONES.length];}
function hopStatus(hop: {last_test_status?: string | null; last_test_error_code?: string | null; last_test_at?: string | null}) {return hop.last_test_status === 'ok' ? 'OK' : hop.last_test_status === 'failed' ? hop.last_test_error_code?.replaceAll('-', ' ') || 'Failed' : 'Not checked';}
function credentialKind(kind?: string | null) {return kind === 'key' ? 'Key' : kind === 'password' ? 'Password' : kind === 'agent' ? 'SSH agent' : 'OpenSSH defaults';}
function scopeLabel(scope?: string) {return scope === 'public' ? 'Public' : scope === 'private' ? 'Private VPC' : scope === 'lan' ? 'LAN' : scope === 'loopback' ? 'This machine' : 'Unresolved';}
function routeFlags(rows: {host: Host; label: string}[], index: number) {
  const host = rows[index].host;
  const parent = host.proxy_jumps.find(jump => rows.slice(0, index).some(row => row.host.alias === jump));
  const isParent = rows.some((row, position) => position > index && row.host.proxy_jumps.includes(host.alias));
  return `${parent ? ' route-child' : ''}${isParent ? ' route-parent' : ''}`;
}

function LegacyCredential({host,credential,runtime,onRefresh}:{host:Host;credential?:Credential;runtime:RuntimeInfo;onRefresh:()=>Promise<void>}) {
  const [saved,setSaved]=useState<Credential>(),[error,setError]=useState<unknown>(),[busy,setBusy]=useState(false);
  const alive=useRef(true);useEffect(()=>()=>{alive.current=false;},[]);
  const close=()=>{location.hash='#/hosts';};
  const bind=async(value:Credential)=>{setBusy(true);setError(undefined);try{await api(`/hosts/${segment(host.id)}`,json('PATCH',{credential_id:value.id}));await onRefresh();if(alive.current)close();}catch(reason){setError(reason);}finally{setBusy(false);}};
  if(saved)return <Modal title={`Use ${saved.label} for ${host.alias}`} onClose={close}><p>The credential was saved. Confirm its binding to this host.</p><ErrorNotice error={error}/><div className="modal-footer"><Button onClick={close}>Close</Button><Button variant="primary" disabled={busy} onClick={()=>void bind(saved)}>{busy?'Binding…':'Retry binding'}</Button></div></Modal>;
  return <CredentialEditor credential={credential} runtime={runtime} initialKind="password" initialLabel={host.alias} saveLabel="Save and use" focusSecret onClose={close} onSaved={async value=>{setSaved(value);await bind(value);}}/>;
}

function HostEditor({host, duplicate, hosts, credentials, onClose, onSaved, onCreateCredential}: {host?: Host; duplicate: boolean; hosts: Host[]; credentials: Credential[]; onClose: () => void; onSaved: () => Promise<void>; onCreateCredential: Props['onCreateCredential']}) {
  const [alias,setAlias] = useState(duplicate ? `${host?.alias}-copy` : host?.alias || ''), [hostname,setHostname] = useState(host?.hostname || ''), [username,setUsername] = useState(host?.username || ''), [port,setPort] = useState(host?.port || 22), [credential,setCredential] = useState(host?.credential_id || ''), [jumps,setJumps] = useState(host?.proxy_jumps.join(', ') || ''), [tags,setTags] = useState(host?.tags.join(', ') || ''), [notes,setNotes] = useState(host?.notes || ''), [country,setCountry] = useState(host?.geo?.country_code || ''), [city,setCity] = useState(host?.geo?.city || ''), [error,setError] = useState<unknown>(), [busy,setBusy] = useState(false);
  const save = async (event: React.FormEvent) => {event.preventDefault();setBusy(true);setError(undefined);try {
    const payload: Record<string, unknown> = {alias, hostname, username, port, credential_id: credential || null, proxy_jumps: jumps.split(',').map(x => x.trim()).filter(Boolean), tags: tags.split(',').map(x => x.trim()).filter(Boolean), notes, geo: country ? {country_code: country.toUpperCase(),city: city || null} : null};
    if (host && !duplicate) {const original = {...host, geo: host.geo ? {country_code:host.geo.country_code,city:host.geo.city || null} : null};Object.keys(payload).forEach(key => {if (JSON.stringify(payload[key]) === JSON.stringify(original[key as keyof typeof original])) delete payload[key];});}
    await api(host && !duplicate ? `/hosts/${segment(host.id)}` : '/hosts', json(host && !duplicate ? 'PATCH' : 'POST',payload)); await onSaved();
  } catch(reason){setError(reason);}finally{setBusy(false);}};
  return <Modal title={host && !duplicate ? `Edit ${host.alias}` : duplicate ? 'Duplicate host' : 'Add host'} onClose={onClose}><form onSubmit={event => void save(event)}><ErrorNotice error={error}/><div className="form-grid"><Field label="Alias"><input required value={alias} onChange={e => setAlias(e.target.value)}/></Field><Field label="Hostname / IP"><input required value={hostname} onChange={e => setHostname(e.target.value)}/></Field><Field label="User"><input required value={username} onChange={e => setUsername(e.target.value)}/></Field><Field label="Port"><input required type="number" min={1} max={65535} value={port} onChange={e => setPort(Number(e.target.value))}/></Field><div className="wide"><Field label="Credential"><select value={credential} onChange={e => setCredential(e.target.value)}><option value="">Default SSH identity</option>{credentials.map(c => <option key={c.id} value={c.id}>{c.label} · {c.kind}</option>)}</select></Field><Button variant="ghost" onClick={() => onCreateCredential(c => setCredential(c.id))}><Icon name="plus"/>New credential</Button></div><div className="wide"><Field label="Jump hosts · comma separated"><input list="jump-aliases" value={jumps} onChange={e => setJumps(e.target.value)}/><datalist id="jump-aliases">{hosts.filter(h => h.id !== host?.id).map(h => <option key={h.id} value={h.alias}/>)}</datalist></Field><small>Managed hosts or aliases from your OpenSSH configuration.</small></div><div className="wide"><Field label="Tags · comma separated"><input value={tags} onChange={e => setTags(e.target.value)}/></Field></div><Field label="Country · two-letter code"><input maxLength={2} value={country} onChange={e => setCountry(e.target.value)}/></Field><Field label="City · optional"><input maxLength={120} value={city} onChange={e => setCity(e.target.value)} disabled={!country}/></Field><div className="wide"><Field label="Notes"><textarea value={notes} onChange={e => setNotes(e.target.value)}/></Field></div></div><div className="modal-footer"><Button onClick={onClose}>Cancel</Button><Button variant="primary" type="submit" disabled={busy}>{busy ? 'Saving…' : 'Save host'}</Button></div></form></Modal>;
}

function HostPreview({host, left, top}: {host: Host; left: number; top: number}) {
  const facts = host.facts, memory = parseMemory(facts?.memory), disk = parseDisk(facts?.disk);
  const load = Number((facts?.load || '').trim().split(/\s+/)[0]), cores = Number(facts?.cpu_cores);
  const loadPercent = Number.isFinite(load) && Number.isFinite(cores) && cores > 0 ? Math.min(100, Math.round(load / cores * 100)) : undefined;
  const tone = host.last_test_status === 'ok' ? 'ok' : host.last_test_status === 'failed' ? 'failed' : '';
  const status = host.last_test_status === 'ok' ? `OK${host.last_test_latency_ms == null ? '' : ` · ${host.last_test_latency_ms} ms`}` : host.last_test_status === 'failed' ? 'Failed' : 'Not checked';
  const route = host.proxy_jumps.length ? `${host.proxy_jumps.length + 1} hops` : 'Direct';
  return <div className="host-preview" id="host-preview" role="tooltip" style={{left, top}}>
    <header className="host-preview-head"><OsTile logo={facts?.os_logo} size={30}/><div className="host-preview-title"><strong>{host.alias}</strong><small className="mono">{host.username}@{host.hostname}:{host.port}</small></div><span className={`status-dot ${tone}`}/><span className="host-preview-state">{status}</span></header>
    <section className="preview-section"><h4 className="preview-section-title">Runtime</h4>
      <div className="preview-row"><span className="preview-label"><Icon name="clock" size={14}/>Last check</span><span className="preview-value">{relativeTime(host.last_test_at)}</span></div>
      <div className="preview-row"><span className="preview-label"><Icon name="arrow" size={14}/>Route</span><span className="preview-value">{route}</span></div>
      {loadPercent !== undefined && <div className="preview-row has-meter"><div className="preview-row-main"><span className="preview-label"><Icon name="cpu" size={14}/>CPU load</span><span className="preview-value">{load.toFixed(2)} · {loadPercent}%</span></div><span className="preview-meter"><span style={{width: `${loadPercent}%`}}/></span><span className="preview-meter-note">{loadPercent}% of {cores} vCPU</span></div>}
      {memory && <div className="preview-row has-meter"><div className="preview-row-main"><span className="preview-label"><Icon name="memory" size={14}/>Memory</span><span className="preview-value">{memory.label}</span></div><span className="preview-meter"><span style={{width: `${memory.percent}%`}}/></span><span className="preview-meter-note">{memory.percent}% used</span></div>}
      {disk && <div className="preview-row has-meter"><div className="preview-row-main"><span className="preview-label"><Icon name="disk" size={14}/>Disk</span><span className="preview-value">{disk.label}</span></div><span className="preview-meter"><span style={{width: `${disk.percent}%`}}/></span><span className="preview-meter-note">{disk.percent}% used</span></div>}
      {loadPercent === undefined && !memory && !disk && <div className="preview-row"><span className="preview-label"><Icon name="pulse" size={14}/>Telemetry</span><span className="preview-value muted">Not observed yet</span></div>}
    </section>
    <section className="preview-section"><h4 className="preview-section-title">Specs</h4>
      <div className="preview-row"><span className="preview-label"><Icon name="terminal" size={14}/>OS</span><span className="preview-value">{facts?.os_pretty || '—'}</span></div>
      <div className="preview-row"><span className="preview-label"><Icon name="cpu" size={14}/>Arch</span><span className="preview-value">{facts?.arch || '—'}</span></div>
      <div className="preview-row"><span className="preview-label"><Icon name="cpu" size={14}/>CPU</span><span className="preview-value">{facts?.cpu_cores ? `${facts.cpu_cores} vCPU` : '—'}</span></div>
      <div className="preview-row"><span className="preview-label"><Icon name="memory" size={14}/>Memory</span><span className="preview-value">{memory?.total || '—'}</span></div>
      <div className="preview-row"><span className="preview-label"><Icon name="globe" size={14}/>Hostname</span><span className="preview-value mono">{facts?.hostname || host.hostname}</span></div>
      <div className="preview-row"><span className="preview-label"><Icon name="key" size={14}/>Credential</span><span className="preview-value">{host.credential_label || 'Default SSH identity'}</span></div>
    </section>
    <footer className="host-preview-foot"><span>Quick preview</span><span>Click the host name for details</span></footer>
  </div>;
}

function HostDetails({host, hosts, onClose, onRefresh, onEdit, onCheck, onObserve, onDiagnose, onFiles, onCopy, onTags, busy}: {host: Host; hosts: Host[]; credentials: Credential[]; onClose: () => void; onRefresh: () => Promise<void>; onEdit: () => void; onCheck: () => void; onObserve: () => void; onDiagnose: () => void; onFiles: () => void; onCopy: () => void; onTags: () => void; busy: boolean}) {
  const {data: workspace,error: workspaceError,reload} = useResource<Workspace>(`/hosts/${segment(host.id)}/workspace`), {data: route,error: routeError,reload:reloadRoute} = useResource<HostRoute>(`/hosts/${segment(host.id)}/route`);
  const [notes,setNotes] = useState(host.notes), [error,setError] = useState<unknown>(), [saving,setSaving] = useState(false), [binding,setBinding] = useDialogState('host-skills',false), [addingPath,setAddingPath] = useState(false), [pathLabel,setPathLabel] = useState(''), [pathValue,setPathValue] = useState('');
  const ref = useRef<HTMLElement>(null), close = useRef(onClose); close.current = onClose;
  useEffect(() => {const previous = document.activeElement as HTMLElement | null;ref.current?.focus();const key = (e: KeyboardEvent) => {if (e.key === 'Escape' && !document.querySelector('.modal-backdrop')) close.current(); if (e.key === 'Tab' && !document.querySelector('.modal-backdrop')) {const nodes = [...ref.current!.querySelectorAll<HTMLElement>('button:not([disabled]),a[href],input,textarea,select')];const first=nodes[0],last=nodes.at(-1);if(e.shiftKey&&(document.activeElement===first||document.activeElement===ref.current)){e.preventDefault();last?.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}}};document.addEventListener('keydown',key);return () => {document.removeEventListener('keydown',key);previous?.isConnected&&previous.focus();};}, []);
  const observedRevision=useRef(host.updated_at);
  useEffect(() => {if(observedRevision.current!==host.updated_at){observedRevision.current=host.updated_at;setNotes(host.notes);reload();reloadRoute();}}, [host.updated_at,reload,reloadRoute,host.notes]);
  const perform = async (task: () => Promise<unknown>) => {setSaving(true);setError(undefined);try {await task();reload();await onRefresh();}catch(reason){setError(reason);}finally{setSaving(false);}};
  const index = hosts.findIndex(value => value.id === host.id), jump = host.proxy_jumps[host.proxy_jumps.length - 1];
  const step = (delta: number) => {if (hosts.length < 2) return;const next = hosts[(index + delta + hosts.length) % hosts.length];location.hash = `#/hosts/${segment(next.alias)}`;};
  const hops = route?.hops || [];
  const memory = parseMemory(host.facts?.memory), disk = parseDisk(host.facts?.disk);
  const systemRows: {label: string; value: string; icon: string; mono?: boolean; meter?: {percent: number; note: string}}[] = [
    {label: 'Kernel', value: host.facts?.kernel || '—', icon: 'terminal', mono: true},
    {label: 'CPU', value: [host.facts?.cpu_cores ? `${host.facts.cpu_cores} vCPU` : '', host.facts?.cpu_model || ''].filter(Boolean).join(' · ') || '—', icon: 'cpu'},
    {label: 'Memory', value: memory?.label || host.facts?.memory || '—', icon: 'memory', meter: memory ? {percent: memory.percent, note: `${memory.percent}% used`} : undefined},
    {label: 'Disk', value: disk?.label || host.facts?.disk || '—', icon: 'disk', meter: disk ? {percent: disk.percent, note: `${disk.percent}% used`} : undefined},
    {label: 'Uptime', value: host.facts?.uptime || '—', icon: 'clock'},
    {label: 'Load', value: host.facts?.load || '—', icon: 'pulse', mono: true},
  ];
  return <><div className="drawer-scrim" onMouseDown={onClose} aria-hidden="true"/><aside className="host-drawer" role="dialog" aria-modal="true" aria-label={`Host details ${host.alias}`} ref={ref} tabIndex={-1}>
    <header className="drawer-topbar">
      <nav className="breadcrumb" aria-label="Breadcrumb"><a href="#/hosts">Hosts</a><span aria-hidden="true">/</span>{jump && <><a href={`#/hosts/${segment(jump)}`}>{jump}</a><span aria-hidden="true">/</span></>}<span className="current">{host.alias}</span></nav>
      <span className="drawer-count">{index + 1} of {hosts.length}</span>
      <Button variant="ghost" aria-label="Previous host" disabled={hosts.length < 2} onClick={() => step(-1)}><Icon name="up"/></Button>
      <Button variant="ghost" aria-label="Next host" disabled={hosts.length < 2} onClick={() => step(1)}><Icon name="arrow"/></Button>
      <span className="drawer-divider" aria-hidden="true"/>
      <Button variant="ghost" aria-label="Close host details" onClick={onClose}><Icon name="close"/></Button>
    </header>
    <div className="drawer-body">
      <ErrorNotice error={error || workspaceError || routeError}/>
      <section className="identity drawer-card drawer-hero">
        <div className="identity-head"><OsTile logo={host.facts?.os_logo} size={44}/><div><h2>{host.alias}</h2><p className="identity-status"><span className={`status-dot ${host.last_test_status === 'ok' ? 'ok' : host.last_test_at ? 'failed' : ''}`}/><strong>{statusLabel(host)}</strong>{host.last_test_at && <span className="muted"> · checked {relativeTime(host.last_test_at)}</span>}</p></div></div>
        <div className="identity-chips">{host.tags.map(name => <span className="tag-chip" key={name}><span className={`tag-dot ${tagTone(name)}`}/>{name}</span>)}{host.facts?.os_pretty && <span className="info-chip">{host.facts.os_pretty}{host.facts.arch ? ` · ${host.facts.arch}` : ''}</span>}<span className="info-chip">{scopeLabel(host.address_scope)}</span><button className="tag-chip empty" onClick={onTags}>+ Tag</button></div>
        <div className="identity-actions"><Button variant="primary" onClick={onFiles}><Icon name="folder"/>Browse files</Button><Button onClick={onCheck} disabled={busy}><Icon name="check"/>Run check</Button><Button onClick={onCopy}><Icon name="terminal"/>Copy SSH</Button><Button onClick={onEdit}><Icon name="edit"/>Edit</Button></div>
      </section>
      <section className="drawer-card"><h3 className="section-heading"><Icon name="arrow" size={14}/>Route</h3>
        <div className="route-box">
          <div className="route-flow">
            <span className="route-node"><Icon name="terminal" size={16}/><span><strong>This Mac</strong><small>OpenSSH client</small></span></span>
            <span className="route-line" aria-hidden="true"/>
            {hops.length ? hops.map((hop, position) => <Fragment key={`${hop.alias}-${position}`}><span className={`route-node${position === hops.length - 1 ? ' current' : ''}`}>{position < hops.length - 1 ? <span className="route-logo"><Icon name="hosts" size={14}/></span> : <Icon name="hosts" size={14}/>}<span><strong>{hop.alias}</strong><small>{hop.external ? 'external alias' : hopStatus(hop)}</small></span></span>{position < hops.length - 1 && <span className="route-line" aria-hidden="true"/>}</Fragment>) : <span className="route-node current"><Icon name="hosts" size={14}/><span><strong>{host.alias}</strong><small>{statusLabel(host)}</small></span></span>}
          </div>
          <p className="route-hint">{hops.length > 1 ? `${hops.length - 1} jump${hops.length > 2 ? 's' : ''} via ProxyJump. Every hop is a saved host.` : 'Direct connection. No ProxyJump hops are configured.'}</p>
          <button className="route-action" onClick={onEdit}>Edit jump hosts</button>
        </div>
      </section>
      <section className="drawer-card"><h3 className="section-heading"><Icon name="terminal" size={14}/>Connection</h3>
        <dl className="detail-rows">
          <dt><Icon name="globe" size={14}/>Endpoint</dt><dd><span className="mono">{host.username}@{host.hostname}:{host.port}</span><span className="info-chip">{scopeLabel(host.address_scope)}</span></dd>
          <dt><Icon name="arrow" size={14}/>Jump via</dt><dd>{host.proxy_jumps.length ? host.proxy_jumps.join(' → ') : <span className="muted">Direct</span>}</dd>
          <dt><Icon name="key" size={14}/>Credential</dt><dd>{host.credential_id ? <a href={`#/credentials/${segment(host.credential_id)}`}>{host.credential_label || 'Saved credential'}</a> : 'Default SSH identity'}<small>{credentialKind(host.credential_kind)}</small></dd>
          <dt><Icon name="lock" size={14}/>Host key</dt><dd>{host.host_key?.state || 'unknown'}{host.host_key?.fingerprint && <small className="mono">{host.host_key.fingerprint}</small>}</dd>
          <dt><Icon name="clock" size={14}/>Last success</dt><dd>{host.last_success_at ? relativeTime(host.last_success_at) : <span className="muted">Never</span>}</dd>
        </dl>
        <div className="terminal-bar"><code>$ serverctl connect {host.alias}</code><Button variant="ghost" aria-label="Copy connect command" onClick={onCopy}><Icon name="copy" size={14}/></Button></div>
      </section>
      <section className="drawer-card"><div className="section-head"><h3 className="section-heading"><Icon name="cpu" size={14}/>System</h3><span className="section-note">{host.facts?.observed_at ? `Last observed · ${relativeTime(host.facts.observed_at)} · ` : 'Not observed yet · '}{host.facts && <><button className="link-button" onClick={onObserve} disabled={busy}>Collect host info</button> · </>}<button className="link-button" onClick={onDiagnose} disabled={busy}>Diagnose again</button></span></div>
        {host.facts ? <div className="system-list">{systemRows.map(row => <div className={`system-row${row.meter ? ' has-meter' : ''}`} key={row.label}><div className="system-row-main"><span className="system-label"><Icon name={row.icon} size={14}/>{row.label}</span><span className={`system-value${row.mono ? ' mono' : ''}`}>{row.value}</span></div>{row.meter && <><span className="segmented-meter" aria-hidden="true"><span style={{width: `${row.meter.percent}%`}}/></span><span className="system-meter-note">{row.meter.note}</span></>}</div>)}</div> : <div className="system-empty"><p>Collect host info to save OS, kernel, CPU, memory, disk and uptime. Connection checks do not collect system info.</p><Button onClick={onObserve} disabled={busy}>Collect host info</Button></div>}
      </section>
      <section className="drawer-card"><div className="section-head"><h3 className="section-heading"><Icon name="folder" size={14}/>Saved directories{workspace?.paths.length ? ` · ${workspace.paths.length}` : ''}</h3><Button variant="ghost" onClick={() => setAddingPath(!addingPath)}><Icon name="plus" size={13}/>Add</Button></div>
        <div className="dir-list">{workspace?.paths.map(path => <div className="dir-row" key={path.id}><div><strong>{path.label}</strong><small className="mono">{path.path}</small></div><div className="dir-actions"><a href={`#/files/${segment(host.id)}?path=${segment(path.path)}`}>Open</a><Button variant="ghost" aria-label={`Delete saved directory ${path.label}`} disabled={saving} onClick={() => void perform(() => api(`/hosts/${segment(host.id)}/paths/${segment(path.id)}`,json('DELETE')))}><Icon name="close" size={13}/></Button></div></div>)}{!workspace?.paths.length && <p className="muted">No saved directories yet.</p>}</div>
        {addingPath && <div className="dir-form"><Field label="Directory label"><input value={pathLabel} onChange={e=>setPathLabel(e.target.value)}/></Field><Field label="Remote path"><input value={pathValue} onChange={e=>setPathValue(e.target.value)}/></Field><Button disabled={saving||!pathLabel||!pathValue} onClick={()=>void perform(async()=>{await api(`/hosts/${segment(host.id)}/paths`,json('POST',{label:pathLabel,path:pathValue}));setAddingPath(false);setPathLabel('');setPathValue('');})}>Save directory</Button></div>}
      </section>
      <div className="notes-skills">
        <section className="notes-column drawer-card"><h3 className="section-heading"><Icon name="edit" size={14}/>Notes</h3><Field label="Host notes"><textarea value={notes} onChange={e=>setNotes(e.target.value)}/></Field><div className="notes-actions"><Button disabled={saving||notes===host.notes} onClick={()=>void perform(()=>api(`/hosts/${segment(host.id)}/notes`,json('PUT',{mode:'set',text:notes})))}>Save notes</Button><Button disabled={saving||!host.notes} onClick={()=>void perform(async()=>{await api(`/hosts/${segment(host.id)}/notes`,json('PUT',{mode:'clear',text:''}));setNotes('');})}>Clear</Button></div></section>
        <section className="skills-column drawer-card"><div className="section-head"><h3 className="section-heading"><Icon name="skills" size={14}/>Skills{workspace?.skills.length ? ` · ${workspace.skills.length}` : ''}</h3><Button variant="ghost" onClick={()=>setBinding(true)}>Attach</Button></div><div className="skill-badges">{workspace?.skills.map(skill=><span className="skill-badge" key={skill.id}><span className="status-dot ok"/>{skill.name}</span>)}{!workspace?.skills.length && <p className="muted">No skills attached.</p>}</div></section>
      </div>
    </div>
    {binding&&<SkillPicker host={host} current={workspace?.skills || []} onClose={()=>setBinding(false)} onSaved={async()=>{reload();setBinding(false);}}/>}
  </aside></>;
}

function SkillPicker({host,current,onClose,onSaved}:{host:Host;current:Skill[];onClose:()=>void;onSaved:()=>Promise<void>}) {
  const {data:skills,error:loadError}=useResource<Skill[]>('/skills');const [ids,setIds]=useState(new Set(current.map(s=>s.id))),[error,setError]=useState<unknown>(),[busy,setBusy]=useState(false);
  return <Modal title={`Skills for ${host.alias}`} onClose={onClose}><ErrorNotice error={error||loadError}/><div className="checklist">{skills?.map(skill=><label key={skill.id}><input type="checkbox" checked={ids.has(skill.id)} onChange={e=>setIds(previous=>{const next=new Set(previous);e.target.checked?next.add(skill.id):next.delete(skill.id);return next;})}/>{skill.name} <span className="muted">{skill.status}</span></label>)}</div><p className="muted">Bindings route agent metadata. They do not install or run anything.</p><div className="modal-footer"><Button onClick={onClose}>Cancel</Button><Button variant="primary" disabled={busy} onClick={()=>{setBusy(true);void api(`/hosts/${segment(host.id)}/skills`,json('PUT',{skill_ids:[...ids]})).then(onSaved).catch(setError).finally(()=>setBusy(false));}}>Save bindings</Button></div></Modal>;
}

function BulkTags({hosts,onClose,onSaved}:{hosts:Host[];onClose:()=>void;onSaved:()=>Promise<void>}) {
  const [text,setText]=useState(hosts.length===1?hosts[0].tags.join(', '):''),[error,setError]=useState<unknown>(),[busy,setBusy]=useState(false);
  return <Modal title={`Replace tags · ${hosts.length} hosts`} onClose={onClose}><p className="muted">All selected hosts will receive the same tags. Empty input clears their tags.</p><Field label="Tags · comma separated"><input value={text} onChange={e=>setText(e.target.value)}/></Field><ErrorNotice error={error}/><div className="modal-footer"><Button onClick={onClose}>Cancel</Button><Button variant="primary" disabled={busy} onClick={()=>{setBusy(true);void (async()=>{for(const host of hosts) await api(`/hosts/${segment(host.id)}`,json('PATCH',{tags:text.split(',').map(t=>t.trim()).filter(Boolean)}));await onSaved();onClose();})().catch(async reason=>{setError(reason);await onSaved();}).finally(()=>setBusy(false));}}>Replace tags</Button></div></Modal>;
}

function ImportDialog({onClose,onSaved}:{onClose:()=>void;onSaved:()=>Promise<void>}) {
  const [config,setConfig]=useState(''),[preview,setPreview]=useState<ImportPreview>(),[overwrite,setOverwrite]=useState(false),[error,setError]=useState<unknown>(),[busy,setBusy]=useState(false);
  const sequence=useRef(0);useEffect(()=>()=>{sequence.current++;},[]);
  const run=async(apply:boolean)=>{const current=++sequence.current;setBusy(true);setError(undefined);try{if(apply){await api('/import/apply',json('POST',{...(config?{config}:{}),overwrite}));await onSaved();if(current===sequence.current)onClose();}else{const result=await api<ImportPreview>('/import/preview',json('POST',config?{config}:{}));if(current===sequence.current)setPreview(result);}}catch(reason){if(current===sequence.current)setError(reason);}finally{setBusy(false);}};
  return <Modal title="Import OpenSSH configuration" onClose={onClose}>
    <div className="stack">
      <Field label="Config path · leave blank for default"><input placeholder="~/.ssh/config" value={config} disabled={busy} onChange={e=>{sequence.current++;setConfig(e.target.value);setPreview(undefined);}}/></Field>
      <ErrorNotice error={error}/>
      {preview&&<>
        <p>{preview.items.length} hosts found</p>
        <div className="checklist">{preview.items.map((entry,i)=><div key={i} className="mono">{entry.server.alias} · {entry.server.hostname} · {entry.action}</div>)}</div>
        {preview.warnings?.map(item=><div className="notice warning" key={`${item.alias}-${item.code}`}>
          <p>{item.alias}: {item.message}</p>
          <p>Source config: <code>{item.source_config}</code></p>
          {item.fallback_config&&<p>Authentication fallback: <code>{item.fallback_config}</code></p>}
          {item.identity_files.length>0&&<p>Configured IdentityFile paths: {item.identity_files.map((path,index)=><span key={index}><code>{path}</code>{index<item.identity_files.length-1?", ":""}</span>)}</p>}
        </div>)}
        {preview.errors.map((item,i)=><p className="notice warning" key={i}>{item.alias}: {item.message}</p>)}
        {preview.skipped_patterns.length>0&&<p className="muted">Skipped patterns: {preview.skipped_patterns.join(", ")}</p>}
        <label><input type="checkbox" checked={overwrite} disabled={busy} onChange={e=>setOverwrite(e.target.checked)}/> Explicitly replace matching aliases</label>
      </>}
    </div>
    <div className="modal-footer"><Button onClick={onClose}>Cancel</Button><Button disabled={busy} onClick={()=>void run(false)}>Preview</Button><Button variant="primary" disabled={busy||!preview} onClick={()=>void run(true)}>Import hosts</Button></div>
  </Modal>;
}
