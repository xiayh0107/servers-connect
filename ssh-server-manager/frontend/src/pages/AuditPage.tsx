import {useMemo, useState} from 'react';
import {useResource} from '../hooks';
import type {AuditEvent, AuditLog} from '../types';
import {Badge, Button, EmptyState, ErrorNotice, Icon, PageHeader} from '../ui';

/** Event kinds grouped into the chips the filters use. */
const GROUPS: {id: string; label: string; chip: string; events: string[]}[] = [
  {id: 'exec', label: 'Commands', chip: 'exec', events: ['exec', 'exec_result']},
  {id: 'reveal', label: 'Secrets', chip: 'reveal', events: ['reveal']},
  {id: 'denied', label: 'Refused', chip: 'denied', events: ['reveal_denied']},
  {id: 'tunnel', label: 'Tunnels', chip: 'tunnel', events: ['tunnel_open', 'tunnel_close']},
  {id: 'session', label: 'Sessions', chip: 'session', events: ['connect', 'session_log']},
  {id: 'mcp', label: 'Agents', chip: '', events: ['mcp_test']},
];
const RANGES = [['7', 'Last 7 days', 7], ['30', 'Last 30 days', 30], ['all', 'Everything', 0]] as const;

/**
 * The local audit log, newest first.
 *
 * Read-only: the browser can look at history but never rewrite it, which is what
 * makes the record worth anything. No row carries a secret — a reveal appears as
 * the credential's label, and credentials inside a recorded command are masked by
 * the server before the event is written.
 */
export default function AuditPage() {
  const [limit, setLimit] = useState(200), [group, setGroup] = useState('all'), [alias, setAlias] = useState('any'), [query, setQuery] = useState(''), [range, setRange] = useState<string>('7');
  const since = useMemo(() => {
    const days = Number(range);
    if (!days) return null;
    return new Date(Date.now() - days * 86_400_000).toISOString();
  }, [range]);
  const path = `/audit?limit=${limit}${since ? `&since=${encodeURIComponent(since)}` : ''}`;
  const {data, error, loading, reload} = useResource<AuditLog>(path);
  const events = data?.events || [];
  const counts = useMemo(() => {
    const tally: Record<string, number> = {};
    for (const event of events) for (const entry of GROUPS) if (entry.events.includes(event.event)) tally[entry.id] = (tally[entry.id] || 0) + 1;
    return tally;
  }, [events]);
  const aliases = useMemo(() => [...new Set(events.map(event => event.alias).filter(Boolean) as string[])].sort(), [events]);
  const filtered = events.filter(event => {
    const wanted = GROUPS.find(entry => entry.id === group);
    if (wanted && !wanted.events.includes(event.event)) return false;
    if (alias !== 'any' && event.alias !== alias) return false;
    const needle = query.toLocaleLowerCase();
    return !needle || `${event.event} ${event.alias || ''} ${event.credential || ''} ${event.command || ''} ${event.tunnel || ''} ${event.actor}`.toLocaleLowerCase().includes(needle);
  });

  return <div className="page audit-page">
    <PageHeader eyebrow="History" title="Audit" description={`${data?.count ?? 0} events in range · every reveal, refusal, command and tunnel this machine recorded · the log notes it rotates itself at 5 MB`}>
      <Button onClick={() => void reload()} disabled={loading}><Icon name="refresh"/>{loading ? 'Refreshing…' : 'Refresh'}</Button>
    </PageHeader>
    <ErrorNotice error={error}/>

    <div className="toolbar">
      <label className="search-box"><Icon name="search"/><input aria-label="Search events" placeholder="Search command, host, or credential" value={query} onChange={event => setQuery(event.target.value)}/></label>
      <div className="segmented" aria-label="Event kind">
        <Button className={group === 'all' ? 'selected' : ''} aria-pressed={group === 'all'} onClick={() => setGroup('all')}>All <span className="faint">{events.length}</span></Button>
        {GROUPS.filter(entry => counts[entry.id]).map(entry => <Button key={entry.id} className={group === entry.id ? 'selected' : ''} aria-pressed={group === entry.id} onClick={() => setGroup(entry.id)}>{entry.label} <span className="faint">{counts[entry.id]}</span></Button>)}
      </div>
      <label className="inline-select"><span className="muted">Host</span>
        <select aria-label="Filter by host" value={alias} onChange={event => setAlias(event.target.value)}>
          <option value="any">Any</option>
          {aliases.map(name => <option key={name} value={name}>{name}</option>)}
        </select>
      </label>
      <div className="segmented" aria-label="Time range">
        {RANGES.map(([value, label]) => <Button key={value} className={range === value ? 'selected' : ''} aria-pressed={range === value} onClick={() => setRange(value)}>{label}</Button>)}
      </div>
    </div>

    {!events.length && !loading ? <EmptyState title="Nothing recorded yet">
      <p>The log fills as soon as you run a command, open a session, reveal a secret, or open a tunnel. History from before this feature was added is not backfilled.</p>
    </EmptyState> : <>
      <div className="table-wrap audit-table">
        <table>
          <thead><tr><th>When</th><th>Event</th><th>Host</th><th>Detail</th><th>By</th><th>Outcome</th></tr></thead>
          <tbody>
            {filtered.map((event, index) => <tr key={`${event.at}-${index}`} className={event.event === 'reveal_denied' ? 'orphan' : undefined}>
              <td>
                <div className="cell-stack">
                  <span className="mono">{clock(event.at)}</span>
                  <small className="muted">{day(event.at)}</small>
                </div>
              </td>
              <td>
                <div className="cell-title">
                  <span className={`event-chip ${chipFor(event)}`}>{labelFor(event)}</span>
                  <strong>{verbFor(event)}</strong>
                </div>
              </td>
              <td>{event.alias ? <strong>{event.alias}</strong> : <span className="faint">—</span>}</td>
              <td>
                <div className="audit-detail">
                  <span className={detailIsMono(event) ? 'mono' : undefined}>{detailFor(event) || '—'}</span>
                  {event.event === 'exec' && /(\*\*\*|--password|-P)/.test(event.command || '') && <Badge>MASKED</Badge>}
                </div>
              </td>
              <td className="muted">{event.actor}</td>
              <td>{outcomeFor(event)}</td>
            </tr>)}
          </tbody>
        </table>
        {!filtered.length && <p className="table-empty muted">No events match this filter.</p>}
      </div>
      <div className="table-footer">
        <span>Newest {filtered.length} of {data?.count ?? 0} in range. The log holds no secret values: a reveal is recorded by credential name, and credentials inside a command are masked before the line is written.</span>
        <span className="muted">{loading ? 'Loading…' : `${data?.segments.length ?? 0} segment(s)`}</span>
      </div>
    </>}
  </div>;
}

function chipFor(event: AuditEvent) {
  return GROUPS.find(entry => entry.events.includes(event.event))?.chip || '';
}

function labelFor(event: AuditEvent) {
  if (event.event === 'reveal_denied') return 'DENIED';
  if (event.event === 'reveal') return 'REVEAL';
  if (event.event === 'exec_result') return 'RESULT';
  if (event.event.startsWith('tunnel_')) return 'TUNNEL';
  if (event.event === 'exec') return 'EXEC';
  if (event.event === 'session_log') return 'SESSION';
  if (event.event === 'connect') return 'CONNECT';
  return event.event.toUpperCase();
}

function verbFor(event: AuditEvent) {
  switch (event.event) {
    case 'exec': return 'Ran a command';
    case 'exec_result': return 'Command finished';
    case 'reveal': return 'Secret revealed';
    case 'reveal_denied': return 'Reveal refused';
    case 'tunnel_open': return 'Tunnel opened';
    case 'tunnel_close': return 'Tunnel closed';
    case 'connect': return 'Session opened';
    case 'session_log': return 'Session recorded';
    case 'mcp_test': return 'Agent ran a check';
    default: return event.event.replace(/_/g, ' ');
  }
}

function detailFor(event: AuditEvent) {
  if (event.event === 'exec') return event.command || '';
  if (event.event === 'reveal' || event.event === 'reveal_denied') {
    return event.credential ? `${event.credential}${event.slot ? ` · ${event.slot}` : ''}` : '';
  }
  if (event.event.startsWith('tunnel_')) return [event.tunnel, event.forward].filter(Boolean).join(' · ');
  if (event.event === 'session_log') return event.path || '';
  return event.host || '';
}

function detailIsMono(event: AuditEvent) {
  return event.event === 'exec' || event.event.startsWith('tunnel_');
}

function outcomeFor(event: AuditEvent) {
  if (event.event === 'reveal_denied') return <Badge tone="failed">refused</Badge>;
  if (event.event === 'exec_result' || event.event === 'exec') {
    const code = event.returncode;
    if (code === null || code === undefined) return <span className="faint">—</span>;
    return <Badge tone={code === 0 ? 'ok' : 'failed'}>{`exit ${code}`}</Badge>;
  }
  if (event.event === 'reveal') return <Badge>ok</Badge>;
  if (event.event === 'tunnel_open' && event.pid) return <span className="mono muted">{`pid ${event.pid}`}</span>;
  return <span className="faint">—</span>;
}

function clock(value: string) {
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? new Date(parsed).toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false}) : '—';
}

function day(value: string) {
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return '—';
  const date = new Date(parsed), today = new Date();
  const sameDay = date.toDateString() === today.toDateString();
  if (sameDay) return 'today';
  const yesterday = new Date(today.getTime() - 86_400_000);
  if (date.toDateString() === yesterday.toDateString()) return 'yesterday';
  return date.toLocaleDateString(undefined, {month: 'short', day: 'numeric'});
}
