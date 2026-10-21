import {fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import {beforeEach, expect, it, vi} from 'vitest';
import {api} from '../api';
import type {AuditEvent, AuditLog} from '../types';
import AuditPage from './AuditPage';

vi.mock('../api', async original => ({...await original<typeof import('../api')>(), api: vi.fn()}));
const mocked = vi.mocked(api);

const now = new Date();
const at = (minutesAgo: number) => new Date(now.getTime() - minutesAgo * 60_000).toISOString();

function event(changes: Partial<AuditEvent> = {}): AuditEvent {
  return {at: at(5), event: 'exec', actor: 'cli', alias: 'prod-web-01', host: 'prod-web-01', command: 'systemctl is-active nginx', returncode: 0, ok: true, ...changes};
}

let events: AuditEvent[];
let log: AuditLog;
beforeEach(() => {
  events = [
    event(),
    event({at: at(10), event: 'reveal', command: null, returncode: null, credential: 'prod root', slot: 'password'}),
    event({at: at(12), event: 'reveal_denied', command: null, returncode: null, credential: 'prod root', slot: 'password', actor: 'web'}),
    event({at: at(30), event: 'tunnel_open', command: null, returncode: null, tunnel: 'db1-local', forward: '127.0.0.1:15432:localhost:5432', pid: 41902, alias: 'db1'}),
    event({at: at(90), event: 'connect', command: null, returncode: null, alias: 'lab-build'}),
    event({at: at(120), event: 'exec', command: 'psql --password=*** -c "select 1"', returncode: null, alias: 'prod-db-01'}),
  ];
  log = {ok: true, path: '/tmp/ssm-501/audit.log', segments: ['/tmp/ssm-501/audit.log'], count: events.length, events};
  history.replaceState({}, '', '#/audit');
  mocked.mockReset();
  mocked.mockImplementation(async path => {
    if (path.startsWith('/audit')) return log;
    throw new Error(`Unexpected request: ${path}`);
  });
});

it('lists events newest first with a human verb and a masked command flagged', async () => {
  render(<AuditPage/>);

  expect((await screen.findAllByText('Ran a command')).length).toBe(2);
  expect(screen.getByText('Secret revealed')).toBeTruthy();
  expect(screen.getByText('Reveal refused')).toBeTruthy();
  expect(screen.getByText('Tunnel opened')).toBeTruthy();
  // A record can carry a command whose credentials were masked before it was
  // written; the row says so instead of pretending nothing was hidden.
  expect(screen.getByText('MASKED')).toBeTruthy();
  // The log path and segment count are metadata, not event payload.
  expect(mocked.mock.calls[0][0]).toMatch(/^\/audit\?limit=200&since=/);
});

it('never claims a secret value, showing the credential label instead', async () => {
  render(<AuditPage/>);
  const revealed = await screen.findByText('Secret revealed');
  const row = revealed.closest('tr') as HTMLElement;
  // The row names the credential and its slot, never the value.
  expect(within(row).getByText(/prod root/)).toBeTruthy();
  expect(within(row).getByText(/password/)).toBeTruthy();
  expect(row.textContent).not.toMatch(/hunter|secret-value/i);
  expect(screen.getByText('refused')).toBeTruthy();
});

it('filters by event group, by host, and by free text', async () => {
  render(<AuditPage/>);
  await screen.findAllByText('Ran a command');

  fireEvent.click(screen.getByRole('button', {name: /Secrets/}));
  expect(screen.getByText('Secret revealed')).toBeTruthy();
  expect(screen.queryByText('Tunnel opened')).toBeNull();

  fireEvent.click(screen.getByRole('button', {name: /^All/}));
  fireEvent.change(screen.getByLabelText('Filter by host'), {target: {value: 'db1'}});
  expect(screen.getByText('Tunnel opened')).toBeTruthy();
  expect(screen.queryByText('Ran a command')).toBeNull();

  fireEvent.change(screen.getByLabelText('Filter by host'), {target: {value: 'any'}});
  fireEvent.change(screen.getByLabelText('Search events'), {target: {value: 'psql'}});
  expect(screen.getByText('MASKED')).toBeTruthy();
  expect(screen.queryByText('Tunnel opened')).toBeNull();
});

it('reloads with a wider range when the range changes', async () => {
  render(<AuditPage/>);
  await screen.findAllByText('Ran a command');
  const first = mocked.mock.calls.at(-1)?.[0] as string;
  fireEvent.click(screen.getByRole('button', {name: 'Everything'}));
  await waitFor(() => expect(mocked.mock.calls.at(-1)?.[0]).not.toBe(first));
  expect(mocked.mock.calls.at(-1)?.[0]).toBe('/audit?limit=200');
});

it('is read-only: no request ever carries a method', async () => {
  render(<AuditPage/>);
  await screen.findAllByText('Ran a command');
  expect(mocked.mock.calls.every(([, options]) => !options?.method)).toBe(true);
});

it('explains an empty log instead of showing an empty table', async () => {
  log = {...log, count: 0, events: []};
  events = [];
  render(<AuditPage/>);
  expect(await screen.findByText('Nothing recorded yet')).toBeTruthy();
  expect(screen.getByText(/History from before this feature was added is not backfilled/)).toBeTruthy();
});
