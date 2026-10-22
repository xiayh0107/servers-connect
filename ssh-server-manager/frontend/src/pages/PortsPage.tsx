import {useMemo, useState} from 'react';
import {useResource} from '../hooks';
import type {Host, Tunnel} from '../types';
import {Badge, Button, EmptyState, ErrorNotice, Icon, PageHeader} from '../ui';

const KIND_LABEL: Record<string, string> = {local: 'LOCAL', remote: 'REMOTE', dynamic: 'DYNAMIC'};
const kindLabel = (kind: string | null) => (kind && KIND_LABEL[kind]) || 'UNKNOWN';

/**
 * Saved port forwards joined with what this machine is actually running.
 *
 * Read-only on purpose: opening or closing a forward stays in the terminal for
 * now, so this page can never start a process on the user's behalf. Every
 * liveness signal comes from the server, which reads the process table — a
 * reload cannot invent a running tunnel.
 */
export default function PortsPage({hosts}: {hosts: Host[]}) {
  const {data: tunnels, error, loading, reload} = useResource<Tunnel[]>('/tunnels');
  const [query, setQuery] = useState(''), [kind, setKind] = useState('all'), [state, setState] = useState('any');
  const rows = tunnels || [];
  const counts = useMemo(() => ({
    all: rows.length,
    local: rows.filter(row => row.kind === 'local').length,
    remote: rows.filter(row => row.kind === 'remote').length,
    dynamic: rows.filter(row => row.kind === 'dynamic').length,
    running: rows.filter(row => row.running).length,
    orphan: rows.filter(row => row.orphan).length,
  }), [rows]);
  const hostDetail = (alias: string | null) => {
    if (!alias) return 'host unknown';
    const host = hosts.find(item => item.alias === alias);
    return host?.facts?.os_pretty || host?.facts?.os_id || 'not checked yet';
  };
  const filtered = rows.filter(row => {
    const needle = query.toLocaleLowerCase();
    const matches = !needle || `${row.name} ${row.server} ${row.forward} ${row.notes}`.toLocaleLowerCase().includes(needle);
    const inKind = kind === 'all' || row.kind === kind;
    const inState = state === 'any' || (state === 'running' ? row.running : state === 'orphan' ? row.orphan : !row.running);
    return matches && inKind && inState;
  });

  return <div className="page ports-page">
    <PageHeader eyebrow="Network" title="Ports" description={`${counts.all} saved · ${counts.running} running on this machine · ${counts.orphan} orphaned process · forwards stay on loopback unless you change the bind address`}>
      <Button onClick={() => void reload()} disabled={loading}><Icon name="refresh"/>{loading ? 'Refreshing…' : 'Refresh'}</Button>
    </PageHeader>
    <ErrorNotice error={error}/>

    <div className="toolbar">
      <label className="search-box"><Icon name="search"/><input aria-label="Search tunnels" placeholder="Search tunnel, host, or port" value={query} onChange={event => setQuery(event.target.value)}/></label>
      <div className="segmented" aria-label="Forward kind">{[
        ['all', 'All', counts.all],
        ['local', 'Local', counts.local],
        ['remote', 'Remote', counts.remote],
        ['dynamic', 'Dynamic', counts.dynamic],
      ].map(([value, label, count]) => <Button key={String(value)} className={kind === value ? 'selected' : ''} aria-pressed={kind === value} onClick={() => setKind(String(value))}>{label} <span className="faint">{count}</span></Button>)}</div>
      <label className="inline-select"><span className="muted">Status</span>
        <select aria-label="Tunnel state" value={state} onChange={event => setState(event.target.value)}>
          <option value="any">Any</option>
          <option value="running">Running</option>
          <option value="stopped">Saved</option>
          <option value="orphan">Orphaned</option>
        </select>
      </label>
    </div>

    {!rows.length && !loading ? <EmptyState title="No saved forwards">
      <p>Save a forward with <code className="mono">serverctl tunnel add</code>, then open it from your terminal or with <code className="mono">serverctl tunnel open</code>. It will show up here with its live state.</p>
    </EmptyState> : <>
      <div className="table-wrap ports-table">
        <table>
          <thead><tr><th>Tunnel</th><th>Host</th><th>Forward</th><th>State</th><th>Listening</th></tr></thead>
          <tbody>
            {filtered.map(tunnel => <tr key={tunnel.id} className={tunnel.orphan ? 'orphan' : undefined}>
              <td>
                <div className="cell-stack">
                  <span className="cell-title">
                    <strong>{tunnel.name}</strong>
                    <Badge tone={tunnel.kind === 'local' ? 'tag' : tunnel.kind === 'remote' ? 'warning' : ''}>{kindLabel(tunnel.kind)}</Badge>
                  </span>
                  <small className="muted">{tunnel.notes || (tunnel.kind ? `${tunnel.kind} forward` : 'profile deleted')}</small>
                </div>
              </td>
              <td>
                <div className="cell-stack">
                  <span className="cell-title"><Icon name="hosts" size={13}/><strong>{tunnel.server || 'unknown'}</strong></span>
                  <small className="muted">{hostDetail(tunnel.server)}</small>
                </div>
              </td>
              <td>
                <div className="cell-stack">
                  <span className="mono">{tunnel.forward || '—'}</span>
                  <small className="muted">{forwardHint(tunnel)}</small>
                </div>
              </td>
              <td>
                <div className="cell-stack">
                  <span className="cell-title"><span className={`status-dot ${stateTone(tunnel)}`} aria-hidden="true"/><span className={tunnel.orphan ? 'danger-text' : undefined}>{stateLabel(tunnel)}</span></span>
                  <small className="muted">{stateDetail(tunnel)}</small>
                </div>
              </td>
              <td>{listeningLabel(tunnel)}</td>
            </tr>)}
          </tbody>
        </table>
        {!filtered.length && <p className="table-empty muted">{loading ? 'Loading forwards…' : 'No forwards match this filter.'}</p>}
      </div>
      <div className="table-footer">
        <span>{counts.running} {counts.running === 1 ? 'forward is' : 'forwards are'} live from this machine and stop when the session ends. Opening and closing stays in your terminal: <code className="mono">serverctl tunnel open NAME</code>.</span>
        <span className="muted">{rows.length} saved</span>
      </div>
    </>}
  </div>;
}

/** Compact uptime: "18 m", "2 h 14 m", "5 d 2 h". */
function duration(seconds: number) {
  const minutes = Math.floor(seconds / 60);
  if (minutes < 1) return 'just started';
  if (minutes < 60) return `${minutes} m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h${minutes % 60 ? ` ${minutes % 60} m` : ''}`;
  const days = Math.floor(hours / 24);
  return `${days} d${hours % 24 ? ` ${hours % 24} h` : ''}`;
}

function stateLabel(tunnel: Tunnel) {
  if (tunnel.orphan) return 'Orphaned';
  return tunnel.running ? 'Running' : 'Saved';
}

function stateTone(tunnel: Tunnel) {
  if (tunnel.orphan) return 'failed';
  return tunnel.running ? 'ok' : '';
}

function stateDetail(tunnel: Tunnel) {
  if (tunnel.orphan) return tunnel.pid ? `pid ${tunnel.pid} · profile deleted` : 'profile deleted';
  if (!tunnel.running) return 'opens on demand';
  const uptime = duration(tunnel.uptime_seconds ?? 0);
  return tunnel.pid ? `${uptime} · pid ${tunnel.pid}` : uptime;
}

function listeningLabel(tunnel: Tunnel) {
  // A record written before the bind was kept says nothing; do not imply either.
  if (!tunnel.bind_host) return <span className="faint">—</span>;
  const loopback = tunnel.bind_host === '127.0.0.1' || tunnel.bind_host === 'localhost' || tunnel.bind_host === '::1';
  if (tunnel.kind === 'remote') return <Badge>{`on ${tunnel.server}`}</Badge>;
  if (loopback) return <Badge tone="ok">Loopback</Badge>;
  return <Badge tone="warning">Exposed</Badge>;
}

function forwardHint(tunnel: Tunnel) {
  if (tunnel.kind === 'dynamic') return 'SOCKS proxy: every host resolves here';
  if (tunnel.kind === 'remote') return tunnel.bind_port ? `port ${tunnel.bind_port} listens on ${tunnel.server}` : `listens on ${tunnel.server}`;
  if (!tunnel.kind) return 'the profile that described this forward was deleted';
  return `${tunnel.remote_port ?? ''} resolves on ${tunnel.server}, not on this machine`.trim();
}
