import {useState, type FormEvent} from 'react';
import {api, json, segment} from '../api';
import {useDialogState, useHash, useResource} from '../hooks';
import type {Context, Host} from '../types';
import {Badge, Button, EmptyState, ErrorNotice, Field, Icon, Modal, PageHeader} from '../ui';

export default function TagsPage({hosts, onRefresh}: {hosts: Host[]; onRefresh: () => Promise<void>}) {
  const {data: tags, error: loadError, loading, reload} = useResource<Context[]>('/contexts');
  const hash = useHash();
  const selectedName = decode(hash.split('?')[0].split('/')[2]);
  const [query, setQuery] = useState(''), [sort, setSort] = useState('name');
  const [creating, setCreating] = useDialogState('tag-new', false), [deleting, setDeleting] = useDialogState<Context | null>('tag-delete', null);
  const [error, setError] = useState<unknown>(), [busy, setBusy] = useState(false);
  const sorted = [...(tags || [])].sort((a, b) => sort === 'count' ? b.count - a.count || a.name.localeCompare(b.name) : a.name.localeCompare(b.name));
  const selected = tags?.find(tag => tag.name === selectedName) || sorted[0];
  const filtered = sorted.filter(tag => tag.name.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  const refresh = async () => {await onRefresh(); reload();};
  const saved = async (tag: Context) => {await refresh(); location.hash = `#/tags/${segment(tag.name)}`;};
  const remove = async () => {
    if (!deleting || busy) return;
    setBusy(true); setError(undefined);
    try {await api(`/contexts/${segment(deleting.name)}`, json('DELETE')); location.hash = '#/tags'; setDeleting(null); await refresh();}
    catch (reason) {setError(reason);} finally {setBusy(false);}
  };
  return <div className="page"><PageHeader title="Tags" description="Group hosts by environment, client, or purpose. A host can carry several tags."><Button variant="primary" onClick={() => setCreating(true)}><Icon name="plus"/>New tag</Button></PageHeader>
    <ErrorNotice error={error || loadError}/>
    <div className="master-detail"><aside className="master-list"><div className="master-toolbar"><label className="search-box"><Icon name="search"/><input aria-label="Search tags" placeholder="Filter tags" value={query} onChange={event => setQuery(event.target.value)}/></label><select aria-label="Sort tags" value={sort} onChange={event => setSort(event.target.value)}><option value="name">Sort: Name</option><option value="count">Sort: Hosts</option></select></div>
      {filtered.map(tag => <a className={`master-item ${selected?.name === tag.name ? 'active' : ''}`} href={`#/tags/${segment(tag.name)}`} key={tag.name} aria-current={selected?.name === tag.name ? 'true' : undefined}><span className="item-icon"><Icon name="tags"/></span><span><strong>{tag.name}</strong>{!tag.count && <small className="muted">Unused</small>}</span><Badge>{tag.count}</Badge></a>)}
      {!filtered.length && <p className="master-empty">{loading ? 'Loading tags…' : query ? 'No matching tags' : 'No tags yet'}</p>}<footer className="master-footer">{tags?.length || 0} tags · hosts can have several tags</footer>
    </aside><section className="detail-panel">{selected ? <TagDetails key={`${selected.name}:${hosts.filter(host => host.tags.includes(selected.name)).map(host => host.id).sort().join(',')}`} tag={selected} hosts={hosts} onSaved={saved} onDelete={() => {setError(undefined); setDeleting(selected);}}/> : <EmptyState title={loading ? 'Loading tags…' : 'Create your first tag'}><p>Create a tag, then choose the hosts that belong to it.</p></EmptyState>}</section></div>
    {creating && <NewTag hosts={hosts} onClose={() => setCreating(false)} onSaved={async tag => {await saved(tag); setCreating(false);}}/>}
    {deleting && <Modal title={`Delete ${deleting.name}?`} onClose={() => setDeleting(null)}><p>This removes the tag from {deleting.count} hosts. The hosts and their connection settings are preserved.</p><ErrorNotice error={error}/><div className="modal-footer"><Button disabled={busy} onClick={() => setDeleting(null)}>Cancel</Button><Button variant="danger" disabled={busy} onClick={() => void remove()}>{busy ? 'Deleting…' : 'Delete tag'}</Button></div></Modal>}
  </div>;
}

function TagDetails({tag, hosts, onSaved, onDelete}: {tag: Context; hosts: Host[]; onSaved: (tag: Context) => Promise<void>; onDelete: () => void}) {
  const originalIds = new Set(hosts.filter(host => host.tags.includes(tag.name)).map(host => host.id));
  const [name, setName] = useState(tag.name), [ids, setIds] = useState(originalIds), [query, setQuery] = useState(''), [filter, setFilter] = useState('all');
  const [error, setError] = useState<unknown>(), [busy, setBusy] = useState(false);
  const changes = hosts.filter(host => ids.has(host.id) !== originalIds.has(host.id));
  const dirty = name !== tag.name || changes.length > 0;
  const filtered = hosts.filter(host => `${host.alias} ${host.username}@${host.hostname} ${host.tags.join(' ')}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()) && (filter === 'all' || (filter === 'tagged' ? ids.has(host.id) : !ids.has(host.id))));
  const save = async (event: FormEvent) => {
    event.preventDefault(); if (busy || !dirty) return;
    setBusy(true); setError(undefined);
    try {const result = await api<Context>(`/contexts/${segment(tag.name)}`, json('PATCH', {new_name: name, server_ids: [...ids]})); await onSaved(result);}
    catch (reason) {setError(reason);} finally {setBusy(false);}
  };
  return <form onSubmit={event => void save(event)}><header className="detail-panel-header"><div><h2>{tag.name}</h2><p>Renaming updates every host that carries it.</p></div><Button variant="danger" disabled={busy} onClick={onDelete}>Delete tag…</Button></header>
    <div className="panel-section"><Field label="Tag name"><input required maxLength={40} disabled={busy} value={name} onChange={event => setName(event.target.value)}/></Field><p className="muted">Up to 40 characters. Commas and control characters are reserved; names are unique without regard to case.</p></div>
    <div className="panel-section"><h3>Hosts with this tag</h3><p className="muted">{ids.size} of {hosts.length} hosts selected · check or uncheck to assign</p><div className="toolbar"><label className="search-box"><Icon name="search"/><input aria-label="Filter tag hosts" placeholder="Filter hosts" value={query} onChange={event => setQuery(event.target.value)}/></label><div className="segmented" aria-label="Tag membership filter">{['all', 'tagged', 'untagged'].map(value => <Button key={value} className={filter === value ? 'selected' : ''} aria-pressed={filter === value} onClick={() => setFilter(value)}>{value[0].toUpperCase() + value.slice(1)}</Button>)}</div></div>
      <div className="table-wrap"><table><thead><tr><th>Host</th><th>Endpoint</th><th>Other tags</th></tr></thead><tbody>{filtered.map(host => <tr key={host.id}><td><label className="checkbox-label"><input type="checkbox" aria-label={`Tag ${host.alias}`} disabled={busy} checked={ids.has(host.id)} onChange={event => setIds(previous => {const next = new Set(previous); event.target.checked ? next.add(host.id) : next.delete(host.id); return next;})}/><strong>{host.alias}</strong>{ids.has(host.id) !== originalIds.has(host.id) && <Badge tone={ids.has(host.id) ? 'ok' : 'failed'}>{ids.has(host.id) ? '+ Adding' : '− Removing'}</Badge>}</label></td><td className="mono muted">{host.username}@{host.hostname}:{host.port}</td><td><div className="tag-row">{host.tags.filter(value => value !== tag.name).map(value => <Badge key={value}>{value}</Badge>)}{!host.tags.some(value => value !== tag.name) && <span className="muted">—</span>}</div></td></tr>)}</tbody></table></div>
      {!filtered.length && <p className="muted">{hosts.length ? 'No matching hosts. Try another filter.' : 'Add a host to start assigning tags.'}</p>}<ErrorNotice error={error}/>
    </div><footer className="panel-actions"><span className="muted">{dirty ? `${changes.length + Number(name !== tag.name)} unsaved ${changes.length + Number(name !== tag.name) === 1 ? 'change' : 'changes'}` : 'All changes saved'}</span><Button disabled={busy || !dirty} onClick={() => {setName(tag.name); setIds(originalIds); setError(undefined);}}>Discard</Button><Button type="submit" variant="primary" disabled={busy || !dirty || !name.trim()}>{busy ? 'Saving…' : 'Save changes'}</Button></footer>
  </form>;
}

function NewTag({hosts, onClose, onSaved}: {hosts: Host[]; onClose: () => void; onSaved: (tag: Context) => Promise<void>}) {
  const [name, setName] = useState(''), [ids, setIds] = useState(new Set<string>()), [error, setError] = useState<unknown>(), [busy, setBusy] = useState(false);
  const save = async (event: FormEvent) => {event.preventDefault(); if (busy) return; setBusy(true); setError(undefined); try {const result = await api<Context>('/contexts', json('POST', {name, server_ids: [...ids]})); await onSaved(result);} catch (reason) {setError(reason);} finally {setBusy(false);}};
  return <Modal title="New tag" onClose={onClose}><form onSubmit={event => void save(event)}><div className="stack"><Field label="Tag name"><input required maxLength={40} disabled={busy} value={name} onChange={event => setName(event.target.value)}/></Field><p className="muted">Names are unique without regard to case. Commas and control characters are reserved.</p><h3>Assigned hosts</h3><div className="checklist">{hosts.map(host => <label key={host.id}><input type="checkbox" aria-label={`Assign ${host.alias}`} disabled={busy} checked={ids.has(host.id)} onChange={event => setIds(previous => {const next = new Set(previous); event.target.checked ? next.add(host.id) : next.delete(host.id); return next;})}/>{host.alias}</label>)}</div><ErrorNotice error={error}/></div><div className="modal-footer"><Button disabled={busy} onClick={onClose}>Cancel</Button><Button type="submit" variant="primary" disabled={busy || !name.trim()}>{busy ? 'Saving…' : 'Save tag'}</Button></div></form></Modal>;
}
function decode(value?: string) {try {return value ? decodeURIComponent(value) : undefined;} catch {return undefined;}}
