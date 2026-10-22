import {fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import {beforeEach, expect, it, vi} from 'vitest';
import {api} from '../api';
import {host} from '../test-fixtures';
import type {Tunnel} from '../types';
import PortsPage from './PortsPage';

vi.mock('../api', async original => ({...await original<typeof import('../api')>(), api: vi.fn()}));
const mocked = vi.mocked(api);
const hosts = [host({alias: 'db1', facts: {observed_at: '2026-10-20T09:00:00Z', os_logo: 'rocky', os_pretty: 'Rocky Linux 9'}}), host({id: 'host-b', alias: 'prod-web-01'})];

function tunnel(changes: Partial<Tunnel> = {}): Tunnel {
  return {
    id: '3f2a9c11-0000-4000-8000-000000000000',
    server: 'db1', name: 'db1-local', kind: 'local', bind_host: '127.0.0.1', bind_port: 15432,
    remote_host: 'localhost', remote_port: 5432, notes: 'primary Postgres',
    forward: '127.0.0.1:15432:localhost:5432', created_at: '2026-10-20T10:00:00Z', updated_at: '2026-10-20T10:00:00Z',
    running: false, pid: null, started_at: null, uptime_seconds: null, orphan: false, ...changes,
  };
}

let tunnels: Tunnel[];
beforeEach(() => {
  tunnels = [
    tunnel({running: true, pid: 41902, uptime_seconds: 8040}),
    tunnel({id: 'b', name: 'web1-http', server: 'prod-web-01', bind_port: 8080, remote_port: 80, forward: '127.0.0.1:8080:localhost:80', notes: '', running: true, pid: 41988, uptime_seconds: 1080}),
    tunnel({id: 'c', name: 'pgadmin', server: 'prod-web-01', bind_host: '0.0.0.0', bind_port: 5050, forward: '0.0.0.0:5050:127.0.0.1:5432', notes: 'database GUI'}),
    tunnel({id: 'd', name: 'db1-reverse', kind: 'remote', bind_port: 8022, remote_port: 22, forward: 'db1:8022:127.0.0.1:22', notes: ''}),
    tunnel({id: 'e', name: 'socks-lab', kind: 'dynamic', remote_host: null, remote_port: null, forward: '127.0.0.1:1080', notes: ''}),
    tunnel({id: 'f', name: 'old-redis', server: 'ghost-host', notes: '', running: true, orphan: true, pid: 31004, uptime_seconds: 259200}),
  ];
  history.replaceState({}, '', '#/ports');
  mocked.mockReset();
  mocked.mockImplementation(async path => {
    if (path === '/tunnels') return tunnels;
    throw new Error(`Unexpected request: ${path}`);
  });
});

it('shows every saved forward with its live state and never mutates anything', async () => {
  render(<PortsPage hosts={hosts}/>);

  expect(await screen.findByText('db1-local')).toBeTruthy();
  expect(screen.getByText('web1-http')).toBeTruthy();
  expect(screen.getByText('old-redis')).toBeTruthy();
  // Two running forwards, one of them an orphan whose profile is gone.
  // Scope to the table: the state <select> also renders these words as options.
  const table = screen.getByRole('table');
  expect(within(table).getAllByText('Running').length).toBe(2);
  expect(within(table).getAllByText('Saved').length).toBe(3);
  expect(within(table).getByText('Orphaned')).toBeTruthy();
  // Liveness detail comes from the server's process table, not from the row.
  expect(screen.getByText(/pid 41902/)).toBeTruthy();
  // Opening and closing stay in the terminal: no request was ever a mutation.
  expect(mocked.mock.calls.every(([, options]) => !options?.method)).toBe(true);
});

it('marks a wildcard bind as exposed and a remote forward as listening on the host', async () => {
  render(<PortsPage hosts={hosts}/>);
  expect(await screen.findByText('Exposed')).toBeTruthy();
  expect(screen.getByText('on db1')).toBeTruthy();
  expect(screen.getAllByText('Loopback').length).toBe(4);
});

it('filters by kind, state, and free text', async () => {
  render(<PortsPage hosts={hosts}/>);
  await screen.findByText('db1-local');

  fireEvent.click(screen.getByRole('button', {name: /Dynamic/}));
  expect(screen.getByText('socks-lab')).toBeTruthy();
  expect(screen.queryByText('db1-local')).toBeNull();

  fireEvent.click(screen.getByRole('button', {name: /^All/}));
  fireEvent.change(screen.getByLabelText('Tunnel state'), {target: {value: 'orphan'}});
  expect(screen.getByText('old-redis')).toBeTruthy();
  expect(screen.queryByText('db1-local')).toBeNull();

  fireEvent.change(screen.getByLabelText('Tunnel state'), {target: {value: 'any'}});
  fireEvent.change(screen.getByLabelText('Search tunnels'), {target: {value: 'pgadmin'}});
  expect(screen.getByText('pgadmin')).toBeTruthy();
  expect(screen.queryByText('web1-http')).toBeNull();
});

it('explains an empty inventory instead of showing an empty table', async () => {
  tunnels = [];
  render(<PortsPage hosts={hosts}/>);
  expect(await screen.findByText('No saved forwards')).toBeTruthy();
  expect(screen.getByText(/serverctl tunnel add/)).toBeTruthy();
});

it('keeps the table usable when the host behind a forward is unknown', async () => {
  render(<PortsPage hosts={hosts}/>);
  await screen.findByText('old-redis');
  // An orphaned profile can outlive its host row; the row must still render a
  // host cell with a plain placeholder rather than dropping the row.
  const row = screen.getByText('old-redis').closest('tr') as HTMLElement;
  expect(within(row).getByText('ghost-host')).toBeTruthy();
  expect(within(row).getByText('not checked yet')).toBeTruthy();
});

it('refreshes on demand', async () => {
  render(<PortsPage hosts={hosts}/>);
  await screen.findByText('db1-local');
  const before = mocked.mock.calls.length;
  fireEvent.click(screen.getByRole('button', {name: /Refresh/}));
  await waitFor(() => expect(mocked.mock.calls.length).toBeGreaterThan(before));
});

it('never prints an internal id in the row', async () => {
  // A truncated profile id used to sit in a trailing column: it is not part of
  // the design and told the reader nothing. Guard against it coming back.
  render(<PortsPage hosts={hosts}/>);
  const table = await screen.findByRole('table');
  expect(within(table).queryByText('3f2a')).toBeNull();
  expect(table.textContent).not.toMatch(/[0-9a-f]{4}\s*$/);
  // Five columns, matching the five the header declares.
  const headers = within(table).getAllByRole('columnheader');
  expect(headers.length).toBe(5);
});
