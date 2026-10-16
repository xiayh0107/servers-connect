import {useEffect, useMemo, useRef, useState, type CSSProperties, type FormEvent} from 'react';
import {api, ApiError, download, json, segment} from '../api';
import {useDialogState} from '../hooks';
import type {DirectoryEntry, DirectoryListing, Host, RuntimeInfo, SavedPath} from '../types';
import {Button, EmptyState, ErrorNotice, Icon, Modal} from '../ui';
import './files.css';

type Props = {hosts: Host[]; runtime: RuntimeInfo; initialHostId?: string; initialPath?: string};
type SortKey = 'name' | 'size' | 'modified' | 'permissions' | 'owner';
type FileTab = {
  id: string; hostId: string; path?: string; filter: string; query: string;
  history: (string | undefined)[]; refresh: number; listing?: DirectoryListing;
  loading: boolean; error?: unknown; selected?: string;
};
type Upload = Readonly<{tabId: string; hostId: string; alias: string; directory: string; path: string; file: File}>;
type Conflict = {upload: Upload; existing?: Record<string, unknown>; rename: string; error?: unknown};
type PathEditor = {hostId: string; alias: string; saved?: SavedPath; label: string; path: string; notes: string; error?: unknown};

let nextTab = 0;
function newTab(hostId: string, path?: string): FileTab {
  return {id: `files-${++nextTab}`, hostId, path, filter: '', query: '', history: [], refresh: 0, loading: true};
}
const hostApi = (hostId: string) => `/hosts/${segment(hostId)}`;
const listingTimeoutMs = 45_000;
const isAbort = (error: unknown) => (error instanceof Error || error instanceof DOMException) && error.name === 'AbortError';
const filenameValid = (name: string) => !!name && name !== '.' && name !== '..' && !/[\\/\x00-\x1f\x7f]/.test(name);
const joinRemote = (directory: string, name: string) => `${directory === '/' ? '' : directory.replace(/\/$/, '')}/${name}`;
function bytes(size: number) {
  if (!Number.isFinite(size) || size < 0) return '—';
  if (size < 1024) return `${size} B`;
  const unit = size < 1024 ** 2 ? 'KiB' : size < 1024 ** 3 ? 'MiB' : 'GiB';
  const divisor = unit === 'KiB' ? 1024 : unit === 'MiB' ? 1024 ** 2 : 1024 ** 3;
  return `${Number((size / divisor).toFixed(1))} ${unit}`;
}
function modified(entry: DirectoryEntry) {
  return entry.modified_at && Number.isFinite(Date.parse(entry.modified_at))
    ? new Date(entry.modified_at).toLocaleString() : entry.modified || '—';
}
function owner(entry: DirectoryEntry) {return entry.owner || (entry.uid != null ? String(entry.uid) : '—');}
function breadcrumb(path: string) {
  const absolute = path.startsWith('/');
  const parts = path.split('/').filter(Boolean);
  const crumbs = absolute ? [{label: '/', path: '/'}] : [];
  parts.forEach((label, index) => crumbs.push({label, path: `${absolute ? '/' : ''}${parts.slice(0, index + 1).join('/')}`}));
  return crumbs.length ? crumbs : [{label: 'Home', path}];
}

export default function FilesPage({hosts, runtime, initialHostId, initialPath}: Props) {
  const initialHost = hosts.find(host => host.id === initialHostId) || hosts[0];
  const [tabs, setTabs] = useState<FileTab[]>(() => initialHost ? [newTab(initialHost.id, initialPath)] : []);
  const [activeId, setActiveId] = useState(() => tabs[0]?.id || '');
  const [paths, setPaths] = useState<Record<string, SavedPath[]>>({});
  const [pathsError, setPathsError] = useState<unknown>();
  const [pathsRefresh, setPathsRefresh] = useState(0);
  const [sort, setSort] = useState<{key: SortKey; descending: boolean}>({key: 'name', descending: false});
  const [showHidden, setShowHidden] = useState(false);
  const [visibleCount, setVisibleCount] = useState(250);
  const [showSaved, setShowSaved] = useState<boolean | null>(null);
  const [compact, setCompact] = useState(() => window.matchMedia?.('(max-width: 1180px)').matches || false);
  const [focus, setFocus] = useState(false);
  const [switcher, setSwitcher] = useDialogState<'switch' | 'new' | null>('files-switcher', null);
  const [hostSearch, setHostSearch] = useState('');
  const [pathEdit, setPathEdit] = useState(false);
  const [pathInput, setPathInput] = useState('');
  const [editor, setEditor] = useDialogState<PathEditor | null>('files-path', null);
  const [deletePath, setDeletePath] = useDialogState<{hostId: string; alias: string; saved: SavedPath; error?: unknown} | null>('files-delete-path', null);
  const [conflict, setConflict] = useDialogState<Conflict | null>('files-conflict', null);
  const [transferBusy, setTransferBusy] = useState(false);
  const [pathBusy, setPathBusy] = useState(false);
  const [transferError, setTransferError] = useState<unknown>();
  const [message, setMessage] = useState('');
  const fileInput = useRef<HTMLInputElement>(null);
  const uploadDestination = useRef<Omit<Upload, 'file' | 'path'> | null>(null);
  const pathField = useRef<HTMLInputElement>(null);
  const mounted = useRef(true);
  const requestSequence = useRef(0);
  const listingRequest = useRef<{key: string; cancel: () => void} | null>(null);
  const tabsRef = useRef(tabs);
  tabsRef.current = tabs;
  const activeTab = tabs.find(tab => tab.id === activeId);
  const host = hosts.find(value => value.id === activeTab?.hostId);
  const listing = activeTab?.listing;
  const currentPath = listing?.path || activeTab?.path || '';
  const context = `${activeId}|${activeTab?.hostId}|${activeTab?.path}|${activeTab?.filter}|${activeTab?.refresh}`;
  const contextRef = useRef(context);
  contextRef.current = context;
  const maxBytes = runtime.capabilities.max_web_transfer_bytes;
  const transfersAvailable = Number.isFinite(maxBytes) && maxBytes > 0;
  const savedVisible = !focus && (showSaved ?? !compact);
  const selected = listing?.entries.find(entry => entry.path === activeTab?.selected);
  const updateTab = (id: string, change: Partial<FileTab>) => setTabs(previous => previous.map(tab => tab.id === id ? {...tab, ...change} : tab));

  useEffect(() => {mounted.current = true; return () => {mounted.current = false;};}, []);
  useEffect(() => {
    const query = window.matchMedia?.('(max-width: 1180px)');
    if (!query) return;
    const changed = () => setCompact(query.matches);
    query.addEventListener('change', changed); return () => query.removeEventListener('change', changed);
  }, []);
  useEffect(() => {
    if (!tabs.length && hosts.length) {const tab = newTab(initialHost?.id || hosts[0].id, initialPath); setTabs([tab]); setActiveId(tab.id);}
  }, [hosts, initialHost, initialPath, tabs.length]);
  useEffect(() => {
    if (!initialHostId || !hosts.some(value => value.id === initialHostId)) return;
    const existing = tabsRef.current.find(tab => tab.hostId === initialHostId);
    if (existing) {
      setActiveId(existing.id);
      if (initialPath !== undefined && existing.path !== initialPath) updateTab(existing.id, {path: initialPath, history: [...existing.history, existing.path], listing: undefined, filter: '', query: '', error: undefined, selected: undefined});
      return;
    }
    const tab = newTab(initialHostId, initialPath); setTabs(previous => [...previous, tab]); setActiveId(tab.id);
  }, [initialHostId, initialPath, hosts.some(value => value.id === initialHostId)]);
  useEffect(() => {
    if (!activeTab || activeTab.filter === activeTab.query) return;
    const id = activeTab.id, filter = activeTab.filter;
    const timer = window.setTimeout(() => updateTab(id, {query: filter}), 300);
    return () => window.clearTimeout(timer);
  }, [activeTab?.id, activeTab?.filter, activeTab?.query]);
  useEffect(() => {
    if (!activeTab || !host || activeTab.filter !== activeTab.query) return;
    const controller = new AbortController();
    const sequence = ++requestSequence.current, key = contextRef.current, tabId = activeTab.id;
    let settled = false;
    let timer: number | undefined;
    const isCurrent = () => mounted.current && sequence === requestSequence.current && key === contextRef.current;
    const finish = () => {
      settled = true;
      window.clearTimeout(timer);
      if (listingRequest.current === request) listingRequest.current = null;
    };
    const cancel = (error: Error) => {
      if (settled || !isCurrent()) return;
      finish();
      updateTab(tabId, {error, loading: false});
      controller.abort();
    };
    const request = {key, cancel: () => cancel(new Error('Folder loading canceled. Retry folder to try again.'))};
    listingRequest.current = request;
    // Bound directory browsing only; transfers keep their existing explicit lifecycle.
    timer = window.setTimeout(() => cancel(new Error('Folder loading timed out after 45 seconds. Retry folder to try again.')), listingTimeoutMs);
    const params = new URLSearchParams();
    if (activeTab.path !== undefined) params.set('path', activeTab.path);
    if (activeTab.query) params.set('name_filter', activeTab.query);
    updateTab(tabId, {loading: true, error: undefined, selected: undefined});
    api<DirectoryListing>(`${hostApi(host.id)}/files${params.size ? `?${params}` : ''}`, {signal: controller.signal})
      .then(result => {
        if (settled || controller.signal.aborted || !isCurrent()) return;
        finish();
        updateTab(tabId, {listing: result, loading: false}); setVisibleCount(250);
      }).catch(error => {
        if (settled || controller.signal.aborted || !isCurrent()) return;
        finish();
        updateTab(tabId, {error: isAbort(error) ? new Error('Folder loading was interrupted. Retry folder to try again.') : error, loading: false});
      });
    return () => {finish(); controller.abort();};
  }, [activeId, activeTab?.hostId, activeTab?.path, activeTab?.query, activeTab?.filter, activeTab?.refresh, !!host]);
  useEffect(() => {
    if (!host) return;
    const controller = new AbortController(), hostId = host.id;
    setPathsError(undefined);
    api<SavedPath[]>(`${hostApi(hostId)}/paths`, {signal: controller.signal})
      .then(result => {if (mounted.current && !controller.signal.aborted) setPaths(previous => ({...previous, [hostId]: result}));})
      .catch(error => {if (mounted.current && !controller.signal.aborted && !isAbort(error)) setPathsError(error);});
    return () => controller.abort();
  }, [host?.id, pathsRefresh]);
  useEffect(() => {if (pathEdit) pathField.current?.focus();}, [pathEdit]);
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {event.preventDefault(); setHostSearch(''); setSwitcher('switch');}
      if (event.key === 'Escape') {setPathEdit(false); if (!switcher && !editor && !conflict && !deletePath) setFocus(false);}
    };
    window.addEventListener('keydown', key); return () => window.removeEventListener('keydown', key);
  }, [switcher, editor, conflict, deletePath]);

  const entries = useMemo(() => {
    const values = (listing?.entries || []).filter(entry => showHidden || !entry.hidden);
    return values.sort((a, b) => {
      if ((a.type === 'directory') !== (b.type === 'directory')) return a.type === 'directory' ? -1 : 1;
      let comparison = 0;
      if (sort.key === 'size') comparison = a.size - b.size;
      else if (sort.key === 'modified') {
        const left = a.modified_at ? Date.parse(a.modified_at) : NaN, right = b.modified_at ? Date.parse(b.modified_at) : NaN;
        if (!Number.isFinite(left) || !Number.isFinite(right)) return Number.isFinite(left) ? -1 : Number.isFinite(right) ? 1 : a.name.localeCompare(b.name);
        comparison = left - right;
      } else comparison = (sort.key === 'owner' ? owner(a) : a[sort.key] || '').localeCompare(sort.key === 'owner' ? owner(b) : b[sort.key] || '', undefined, {numeric: true, sensitivity: 'base'});
      return (comparison || a.name.localeCompare(b.name)) * (sort.descending ? -1 : 1);
    });
  }, [listing, showHidden, sort]);

  function navigate(path?: string) {
    if (!activeTab) return;
    updateTab(activeTab.id, {path, history: [...activeTab.history, activeTab.path], filter: '', query: '', listing: undefined, selected: undefined, error: undefined});
    setPathEdit(false); setTransferError(undefined); setMessage('');
  }
  function chooseHost(hostId: string) {
    if (switcher === 'new' || !activeTab) {const tab = newTab(hostId); setTabs(previous => [...previous, tab]); setActiveId(tab.id);}
    else if (hostId !== activeTab.hostId) updateTab(activeTab.id, {...newTab(hostId), id: activeTab.id});
    setSwitcher(null); setPathEdit(false); setTransferError(undefined); setMessage('');
  }
  function closeTab(id: string) {
    const remaining = tabs.filter(tab => tab.id !== id); setTabs(remaining);
    if (id === activeId) setActiveId(remaining[Math.max(0, tabs.findIndex(tab => tab.id === id) - 1)]?.id || '');
  }
  function toggleSort(key: SortKey) {setSort(previous => ({key, descending: previous.key === key && !previous.descending}));}
  function refreshOrigin(upload: Upload) {
    setTabs(previous => previous.map(tab => tab.id === upload.tabId && tab.hostId === upload.hostId && (tab.listing?.path || tab.path) === upload.directory ? {...tab, refresh: tab.refresh + 1} : tab));
  }
  async function sendUpload(upload: Upload, overwrite = false) {
    setTransferBusy(true); setTransferError(undefined); setMessage('');
    const form = new FormData(); form.set('file', upload.file); form.set('path', upload.path); form.set('overwrite', String(overwrite));
    try {
      await api(`${hostApi(upload.hostId)}/upload`, {method: 'POST', body: form});
      if (!mounted.current) return;
      setConflict(null); setMessage(`Uploaded ${upload.file.name} to ${upload.alias}:${upload.path}`); refreshOrigin(upload);
    } catch (error) {
      if (!mounted.current) return;
      if (error instanceof ApiError && error.status === 409 && error.code === 'exists') setConflict({upload, existing: error.existing, rename: upload.path.split('/').at(-1) || upload.file.name});
      else {
        const contextual = new Error(`${upload.alias}:${upload.path} — ${error instanceof Error ? error.message : String(error)}`);
        if (conflict) setConflict(previous => previous ? {...previous, error: contextual} : previous);
        else setTransferError(contextual);
      }
    } finally {if (mounted.current) setTransferBusy(false);}
  }
  function selectUpload(file?: File) {
    if (fileInput.current) fileInput.current.value = '';
    const destination = uploadDestination.current || (host && activeTab && listing && !staleListing ? {tabId: activeTab.id, hostId: host.id, alias: host.alias, directory: listing.path} : null);
    uploadDestination.current = null;
    if (!file || !destination) return;
    if (!filenameValid(file.name)) {setTransferError(new Error('The filename cannot contain a slash, a control character, or be . or ..')); return;}
    if (!transfersAvailable || file.size > maxBytes) {setTransferError(new Error(`Web uploads are limited to ${bytes(maxBytes)}. Use serverctl put for larger files.`)); return;}
    const upload = Object.freeze({...destination, path: joinRemote(destination.directory, file.name), file});
    void sendUpload(upload);
  }
  async function getFile(entry: DirectoryEntry) {
    if (!host || transferBusy || entry.type !== 'file' || !transfersAvailable || !Number.isFinite(entry.size) || entry.size < 0 || entry.size > maxBytes) return;
    const hostId = host.id, alias = host.alias, path = entry.path;
    setTransferBusy(true); setTransferError(undefined); setMessage('');
    try {
      const result = await download(`${hostApi(hostId)}/download`, {path});
      if (!mounted.current) return;
      const url = URL.createObjectURL(result.blob);
      const anchor = document.createElement('a'); anchor.href = url; anchor.download = result.filename; document.body.append(anchor); anchor.click(); anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
      setMessage(`Downloaded ${alias}:${path}`);
    } catch (error) {if (mounted.current) setTransferError(new Error(`${alias}:${path} — ${error instanceof Error ? error.message : String(error)}`));}
    finally {if (mounted.current) setTransferBusy(false);}
  }
  async function visitSaved(saved: SavedPath) {
    if (!host) return;
    const hostId = host.id; navigate(saved.path);
    try {
      const result = await api<SavedPath>(`${hostApi(hostId)}/paths/${segment(saved.id)}/used`, {method: 'POST'});
      if (mounted.current) setPaths(previous => ({...previous, [hostId]: (previous[hostId] || []).map(path => path.id === result.id ? result : path)}));
    } catch (error) {if (mounted.current) setTransferError(error);}
  }
  async function savePath(event: FormEvent) {
    event.preventDefault(); if (!editor || pathBusy) return;
    const operation = editor;
    if (!operation.label.trim() || !operation.path) {setEditor({...operation, error: new Error('Enter a label and a remote path.')}); return;}
    setPathBusy(true);
    try {
      const result = await api<SavedPath>(`${hostApi(operation.hostId)}/paths${operation.saved ? `/${segment(operation.saved.id)}` : ''}`, json(operation.saved ? 'PATCH' : 'POST', {label: operation.label.trim(), path: operation.path, notes: operation.notes}));
      if (!mounted.current) return;
      setPaths(previous => ({...previous, [operation.hostId]: operation.saved ? (previous[operation.hostId] || []).map(saved => saved.id === result.id ? result : saved) : [...(previous[operation.hostId] || []), result]}));
      setEditor(null); setMessage(`Saved path for ${operation.alias}`);
    } catch (error) {if (mounted.current) setEditor({...operation, error});}
    finally {if (mounted.current) setPathBusy(false);}
  }
  async function removePath() {
    if (!deletePath || pathBusy) return;
    const operation = deletePath; setPathBusy(true);
    try {
      const result = await api<SavedPath[]>(`${hostApi(operation.hostId)}/paths/${segment(operation.saved.id)}`, {method: 'DELETE'});
      if (mounted.current) {setPaths(previous => ({...previous, [operation.hostId]: result})); setDeletePath(null);}
    } catch (error) {if (mounted.current) setDeletePath({...operation, error});}
    finally {if (mounted.current) setPathBusy(false);}
  }

  if (!hosts.length) return <EmptyState title="No hosts yet">Add a host to browse its remote files.</EmptyState>;
  const staleListing = !activeTab?.error && (activeTab?.loading || activeTab?.filter !== activeTab?.query);
  const hiddenCount = listing?.entries.filter(entry => entry.hidden).length || 0;
  return <section className={`files-page${focus ? ' files-focus' : ''}`} aria-label="Files">
    <div className="files-tabs" role="tablist" aria-label="File sessions">
      {tabs.map((tab, index) => {
        const value = hosts.find(item => item.id === tab.hostId);
        return <div className={`files-tab${tab.id === activeId ? ' active' : ''}`} key={tab.id} style={{'--tab-color': `var(--color-tag-${['rose', 'amber', 'blue', 'green'][index % 4]})`} as CSSProperties}>
          <button role="tab" id={`files-tab-${tab.id}`} aria-controls="files-content" aria-selected={tab.id === activeId} tabIndex={tab.id === activeId ? 0 : -1} onClick={() => {setActiveId(tab.id); setPathEdit(false);}} onKeyDown={event => {
            const target = event.key === 'ArrowRight' ? (index + 1) % tabs.length : event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : -1;
            if (target < 0) return;
            event.preventDefault(); setActiveId(tabs[target].id); setPathEdit(false); document.getElementById(`files-tab-${tabs[target].id}`)?.focus();
          }}><span className="files-status-dot"/><strong>{value?.alias || 'Unavailable host'}</strong><span className="mono muted">{tab.listing?.path.split('/').filter(Boolean).at(-1) || '~'}</span></button>
          {tabs.length > 1 && <button className="files-icon-button" aria-label={`Close ${value?.alias || 'host'} tab`} onClick={() => closeTab(tab.id)}><Icon name="close" size={12}/></button>}
        </div>;
      })}
      <button className="files-icon-button" aria-label="New file tab" onClick={() => {setHostSearch(''); setSwitcher('new');}}><Icon name="plus" size={14}/></button>
      <span className="files-tabs-caption">Remote files</span>
    </div>
    {host && activeTab ? <>
      <header className="files-header">
        <div className="files-identity">
          <div className="files-connection">Files <span>·</span> <span className="mono">{host.username}@{host.hostname}</span>{host.proxy_jumps.length > 0 && <> <span>via</span> <span className="mono">{host.proxy_jumps.join(' → ')}</span></>}</div>
          <button className="files-host-switch" aria-label="Switch file host" onClick={() => {setHostSearch(''); setSwitcher('switch');}}>
            <span className="files-host-icon"><Icon name="hosts" size={18}/></span><h1>{host.alias}</h1>
            {listing && !staleListing && !activeTab.error && <span className="badge success">Checked · {listing.latency_ms} ms</span>}
            {host.tags[0] && <span className="badge">{host.tags[0]}</span>}<Icon name="arrow" size={14}/><kbd>⌘K</kbd>
          </button>
        </div>
        <div className="actions files-header-actions">
          <Button aria-pressed={savedVisible} onClick={() => {if (focus) setFocus(false); setShowSaved(!savedVisible);}}><Icon name="files"/>Saved paths</Button>
          <Button aria-pressed={focus} onClick={() => setFocus(previous => !previous)}>{focus ? 'Exit focus' : 'Focus'}</Button>
          <Button disabled={!!staleListing} onClick={() => updateTab(activeTab.id, {refresh: activeTab.refresh + 1})}><Icon name="refresh" size={14}/>Refresh</Button>
          <Button variant="primary" disabled={transferBusy || !!staleListing || !!activeTab.error || !listing || !transfersAvailable} onClick={() => {if (listing) uploadDestination.current = {tabId: activeTab.id, hostId: host.id, alias: host.alias, directory: listing.path}; fileInput.current?.click();}}><Icon name="upload" size={14}/>{transferBusy ? 'Transferring…' : 'Upload here'}</Button>
          <input ref={fileInput} type="file" aria-label="Upload file" className="files-file-input" onChange={event => selectUpload(event.target.files?.[0])}/>
        </div>
      </header>
      <div className="files-pathbar">
        <div className="files-navigation">
          <Button variant="ghost" aria-label="Back to previous folder" disabled={!activeTab.history.length} onClick={() => {const history = [...activeTab.history]; const path = history.pop(); updateTab(activeTab.id, {path, history, listing: undefined, filter: '', query: '', error: undefined});}}><span aria-hidden="true">‹</span></Button>
          <Button variant="ghost" aria-label="Parent folder" disabled={!listing?.parent || !!staleListing} onClick={() => navigate(listing?.parent || undefined)}><Icon name="up" size={14}/></Button>
        </div>
        {pathEdit ? <form className="files-path-entry" onSubmit={event => {event.preventDefault(); if (pathInput) navigate(pathInput);}}><input ref={pathField} aria-label="Remote directory path" value={pathInput} onChange={event => setPathInput(event.target.value)}/><Button type="submit">Go</Button></form> : <div className="files-breadcrumbs" aria-label="Remote directory">
          {breadcrumb(currentPath).map((crumb, index) => <span key={`${crumb.path}-${index}`}>{index > 1 && <span className="muted">/</span>}<button disabled={!!staleListing} onClick={() => navigate(crumb.path)}>{crumb.label}</button></span>)}
          <button className="files-edit-path" aria-label="Edit remote path" onClick={() => {setPathInput(currentPath || '~'); setPathEdit(true);}}><Icon name="edit" size={13}/></button>
        </div>}
        <label className="files-filter"><Icon name="search" size={13}/><input aria-label="Filter this folder" placeholder="Filter this folder" value={activeTab.filter} onChange={event => updateTab(activeTab.id, {filter: event.target.value})}/></label>
        <Button aria-pressed={showHidden} onClick={() => setShowHidden(previous => !previous)}>Hidden files{hiddenCount ? ` (${hiddenCount})` : ''}</Button>
        <Button disabled={!listing || !!staleListing} onClick={() => setEditor({hostId: host.id, alias: host.alias, label: currentPath.split('/').filter(Boolean).at(-1) || 'Home', path: currentPath, notes: ''})}>☆ Save path</Button>
      </div>
      <div className="files-notices"><ErrorNotice error={transferError}/>{message && <div role="status" className="notice success">{message}</div>}</div>
      <div className="files-body" id="files-content" role="tabpanel" aria-labelledby={`files-tab-${activeId}`} tabIndex={0}>
        <aside className={`files-saved${showSaved === false || focus ? ' collapsed' : ''}${showSaved === true ? ' explicitly-open' : ''}`} aria-label="Saved paths">
          <h2>Saved paths</h2><ErrorNotice error={pathsError}/>{!!pathsError && <Button onClick={() => setPathsRefresh(value => value + 1)}>Retry saved paths</Button>}
          {(paths[host.id] || []).map(saved => <div key={saved.id} className={`files-saved-row${saved.path === currentPath ? ' active' : ''}`}>
            <button className="files-saved-open" onClick={() => void visitSaved(saved)} title={saved.notes || saved.path}><strong>{saved.label}</strong><span>{saved.path}</span></button>
            <div className="files-saved-actions"><button aria-label={`Edit saved path ${saved.label}`} onClick={() => setEditor({hostId: host.id, alias: host.alias, saved, label: saved.label, path: saved.path, notes: saved.notes})}><Icon name="edit" size={12}/></button><button aria-label={`Delete saved path ${saved.label}`} onClick={() => setDeletePath({hostId: host.id, alias: host.alias, saved})}><Icon name="close" size={12}/></button></div>
          </div>)}
          {!paths[host.id]?.length && !pathsError && <p className="files-saved-empty">Save a folder to return to it quickly.</p>}
          <h2 className="files-places-heading">Places</h2><button className="files-place" onClick={() => navigate()}>Home <span>~</span></button><button className="files-place" onClick={() => navigate('/')}>Root <span>/</span></button>
          <p className="files-transfer-hint">Web transfers up to {bytes(maxBytes)}.<br/>Larger files: <span className="mono">serverctl get / put</span>.</p>
        </aside>
        <div className="files-list">
          <ErrorNotice error={activeTab.error}/>{!!activeTab.error && <div className="files-retry"><Button onClick={() => updateTab(activeTab.id, {refresh: activeTab.refresh + 1})}>Retry folder</Button></div>}
          {staleListing ? <div className="files-loading" role="status">{activeTab.filter !== activeTab.query ? 'Waiting for search…' : <>Loading folder… <Button onClick={() => {if (listingRequest.current?.key === contextRef.current) listingRequest.current.cancel();}}>Cancel loading</Button></>}</div> : !activeTab.error && listing && <>
            <div className="files-table-wrap"><table className="files-table" aria-label="Remote files">
              <thead><tr><th className="files-name-column" aria-sort={sort.key === 'name' ? sort.descending ? 'descending' : 'ascending' : 'none'}><button onClick={() => toggleSort('name')}>Name{sort.key === 'name' ? sort.descending ? ' ↓' : ' ↑' : ''}</button></th>{(['size', 'modified', 'permissions', 'owner'] as SortKey[]).map(key => <th className={`files-${key}-column`} key={key} aria-sort={sort.key === key ? sort.descending ? 'descending' : 'ascending' : 'none'}><button onClick={() => toggleSort(key)}>{key === 'permissions' ? 'Mode' : key}{sort.key === key ? sort.descending ? ' ↓' : ' ↑' : ''}</button></th>)}<th className="files-action-column"><span className="sr-only">Actions</span></th></tr></thead>
              <tbody>{listing.parent && <tr><td><button className="files-name" onClick={() => navigate(listing.parent || undefined)}><Icon name="up"/><span>..</span></button></td><td colSpan={5}/></tr>}{entries.slice(0, visibleCount).map(entry => {
                const knownSize = Number.isFinite(entry.size) && entry.size >= 0;
                const allowed = entry.type === 'file' && transfersAvailable && knownSize && entry.size <= maxBytes;
                const restriction = entry.type === 'symlink' ? 'Symlink target size cannot be verified. Use serverctl get.' : entry.type === 'special' ? 'Special files cannot be downloaded in the browser.' : !knownSize ? 'File size unavailable · use CLI' : entry.size > maxBytes ? `Over ${bytes(maxBytes)} · use CLI` : !transfersAvailable ? 'Web transfer limit unavailable.' : '';
                return <tr key={entry.path} className={selected?.path === entry.path ? 'selected' : ''} onClick={() => updateTab(activeTab.id, {selected: entry.path})}>
                  <td><button className="files-name" onClick={() => entry.type === 'directory' ? navigate(entry.path) : updateTab(activeTab.id, {selected: entry.path})}><span className={`files-entry-icon ${entry.type}`}><Icon name={entry.type === 'directory' ? 'folder' : entry.type === 'symlink' ? 'link' : 'file'}/></span><strong>{entry.name}</strong>{entry.type === 'symlink' && <span className="files-link-target">→ {entry.link_target || 'target unavailable'}</span>}</button>{restriction && entry.type === 'file' && <span className="files-limit-badge">{restriction}</span>}</td>
                  <td className="files-size-column">{entry.type === 'file' ? bytes(entry.size) : '—'}</td><td className="files-modified-column" title={entry.modified_at || 'Exact timestamp unavailable'}>{modified(entry)}</td><td className="files-permissions-column mono">{entry.permissions || '—'}</td><td className="files-owner-column" title={`Group: ${entry.group || (entry.gid != null ? String(entry.gid) : '—')}`}>{owner(entry)}</td>
                  <td className="files-action-column">{entry.type !== 'directory' && <button className="files-icon-button" aria-label={`Download ${entry.name}`} disabled={!allowed || transferBusy} title={restriction || `Download ${entry.name}`} onClick={event => {event.stopPropagation(); void getFile(entry);}}><Icon name="download" size={16}/></button>}</td>
                </tr>;
              })}</tbody>
            </table></div>
            {!entries.length && <EmptyState title={activeTab.query ? 'No matching files' : 'This folder is empty'}>{hiddenCount > 0 && 'Enable hidden files to show the hidden entries returned for this folder.'}</EmptyState>}
            {entries.length > visibleCount && <div className="files-show-more"><Button onClick={() => setVisibleCount(value => value + 250)}>Show next {Math.min(250, entries.length - visibleCount)} entries</Button></div>}
            <div className="files-counts" role="status">Showing {Math.min(visibleCount, entries.length)} of {listing.entries.length} returned · {listing.matching_total != null ? `${listing.matching_total} matches` : 'match count unavailable'} · {listing.total} parsed entries{hiddenCount > 0 && !showHidden ? ` · ${hiddenCount} returned hidden entries hidden` : ''}</div>
            {listing.truncated && <p className="files-warning">Results are truncated. Filter this folder to search all parsed names; sorting applies to the returned entries.</p>}{listing.unparsed > 0 && <p className="files-warning">{listing.unparsed} entries could not be parsed. Directory counts may be incomplete.</p>}
            {selected && <dl className="files-selection" aria-label="Selected file metadata"><div><dt>Path</dt><dd className="mono">{selected.path}</dd></div><div><dt>Type</dt><dd>{selected.type}</dd></div><div><dt>Group</dt><dd>{selected.group || (selected.gid != null ? String(selected.gid) : '—')}</dd></div>{selected.type === 'symlink' && <div><dt>Target</dt><dd className="mono">{selected.link_target || 'Unavailable'}</dd></div>}</dl>}
          </>}
        </div>
      </div>
    </> : <EmptyState title="This host is unavailable"><Button onClick={() => setSwitcher('switch')}>Choose a host</Button></EmptyState>}
    {switcher && <Modal title={switcher === 'new' ? 'Open a file tab' : 'Switch file host'} onClose={() => setSwitcher(null)}><label className="field"><span>Find a host</span><input data-initial-focus="" value={hostSearch} onChange={event => setHostSearch(event.target.value)} placeholder="Alias, address or tag"/></label><div className="files-host-options">{hosts.filter(value => `${value.alias} ${value.hostname} ${value.tags.join(' ')}`.toLocaleLowerCase().includes(hostSearch.toLocaleLowerCase())).map(value => <button key={value.id} onClick={() => chooseHost(value.id)}><Icon name="hosts"/><span><strong>{value.alias}</strong><small>{value.username}@{value.hostname}{value.proxy_jumps.length ? ` via ${value.proxy_jumps.join(' → ')}` : ''}</small></span>{value.id === host?.id && <span className="badge">Current</span>}</button>)}</div></Modal>}
    {editor && <Modal title={editor.saved ? 'Edit saved path' : 'Save path'} onClose={() => {if (!pathBusy) setEditor(null);}}><form onSubmit={savePath}><p className="muted">{editor.alias}</p><ErrorNotice error={editor.error}/><label className="field"><span>Label</span><input required value={editor.label} disabled={pathBusy} onChange={event => setEditor({...editor, label: event.target.value})}/></label><label className="field"><span>Remote path</span><input required className="mono" value={editor.path} disabled={pathBusy} onChange={event => setEditor({...editor, path: event.target.value})}/></label><label className="field"><span>Notes</span><textarea value={editor.notes} disabled={pathBusy} onChange={event => setEditor({...editor, notes: event.target.value})}/></label><div className="actions"><Button disabled={pathBusy} onClick={() => setEditor(null)}>Cancel</Button><Button type="submit" variant="primary" disabled={pathBusy}>{pathBusy ? 'Saving…' : 'Save path'}</Button></div></form></Modal>}
    {deletePath && <Modal title="Delete saved path" onClose={() => {if (!pathBusy) setDeletePath(null);}}><p>Remove <strong>{deletePath.saved.label}</strong> from {deletePath.alias}?</p><p className="mono">{deletePath.saved.path}</p><ErrorNotice error={deletePath.error}/><div className="actions"><Button disabled={pathBusy} onClick={() => setDeletePath(null)}>Cancel</Button><Button variant="danger" disabled={pathBusy} onClick={() => void removePath()}>Delete saved path</Button></div></Modal>}
    {conflict && <Modal title="File already exists" onClose={() => {if (!transferBusy) setConflict(null);}}><p><strong>{conflict.upload.alias}</strong>: <span className="mono">{conflict.upload.path}</span></p><p>The remote target changed or already exists. Choose a new filename, cancel, or explicitly overwrite this target.</p>{conflict.existing && <dl className="files-conflict-details"><div><dt>Existing type</dt><dd>{String(conflict.existing.type || 'Unavailable')}</dd></div><div><dt>Size</dt><dd>{typeof conflict.existing.size === 'number' ? bytes(conflict.existing.size) : '—'}</dd></div><div><dt>Modified</dt><dd>{String(conflict.existing.modified_at || conflict.existing.modified || '—')}</dd></div></dl>}<ErrorNotice error={conflict.error}/><label className="field"><span>New filename</span><input value={conflict.rename} disabled={transferBusy} onChange={event => setConflict({...conflict, rename: event.target.value, error: undefined})}/></label><div className="actions"><Button disabled={transferBusy} onClick={() => setConflict(null)}>Cancel</Button><Button disabled={transferBusy || !filenameValid(conflict.rename) || joinRemote(conflict.upload.directory, conflict.rename) === conflict.upload.path} onClick={() => void sendUpload(Object.freeze({...conflict.upload, path: joinRemote(conflict.upload.directory, conflict.rename)}))}>Upload with new name</Button><Button variant="danger" disabled={transferBusy} onClick={() => void sendUpload(conflict.upload, true)}>Overwrite this file</Button></div><p className="files-dialog-hint">The original file and destination stay fixed even if you switch tabs or hosts.</p></Modal>}
  </section>;
}
