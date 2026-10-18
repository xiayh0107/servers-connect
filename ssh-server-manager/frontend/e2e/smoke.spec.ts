import {test, expect, type Locator, type Page} from '@playwright/test';

test.setTimeout(30_000);

const observed = '2026-10-15T12:00:00Z';
const host = (alias: string, id: string, jumps: string[] = []) => ({
  id, alias, hostname: `${alias}.example.invalid`, username: 'deploy', port: 22,
  credential_id: 'credential-key', credential_kind: 'key', credential_label: 'Deployment key', proxy_jumps: jumps,
  tags: ['production'], notes: 'Browser acceptance fixture', source: 'manual', created_at: observed, updated_at: observed,
  last_test_at: observed, last_test_status: 'ok', last_test_error_code: null, last_test_latency_ms: 24,
  last_test_message: 'Connection checked', last_success_at: observed, last_resolved_ip: '203.0.113.4', address_scope: 'public',
  jump_for_count: 0, facts: {os_logo: 'ubuntu', os_pretty: 'Ubuntu 24.04 LTS', architecture: 'x86_64', observed_at: observed,
    kernel: 'Linux 6.8.0-41-generic', arch: 'x86_64', cpu_cores: '8', cpu_model: 'AMD EPYC 7B13',
    memory: 'Mem: 30.5Gi 11.2Gi 19.3Gi 0B 0B', disk: '/dev/nvme0n1p1 460G 120G 340G 27% /',
    uptime: 'up 14 days, 3 hours', load: '0.42 0.38 0.35'},
  geo: {country_code: 'US', city: 'Ashburn', source: 'manual'}, host_key: {state: 'verified', fingerprint: 'SHA256:browser-fixture', observed_at: observed},
});
const hosts = [host('prod-web-01', 'host/#one'), host('stage-api-01', 'host-two', ['external-jump']), host('build-agent-01', 'host-three')];
const credential = {id: 'credential-key', label: 'Deployment key', kind: 'key', key_path: '/fixture/keys/deploy_ed25519',
  has_secret: false, has_passphrase: true, created_at: observed, updated_at: observed, last_test: null,
  key_metadata: {path: '/fixture/keys/deploy_ed25519', status: 'valid', format: 'openssh', encrypted: true,
    algorithm: 'Ed25519', fingerprint: 'SHA256:browser-fixture', bits: 256,
    permissions: {method: 'posix_mode', assessment: 'secure', mode: '0600'}, observed_at: observed}};
const runtime = {api_version: '1', version: '0.9.0', ui: 'modern', shell: 'web', csrf: 'browser-fixture-csrf', server_time: observed,
  auth: {master_password_enrolled: false, passkeys: 0, webauthn_available: false},
  session: {expires_at: new Date(Date.now() + 8 * 60 * 60 * 1000).toISOString()}, vault: {name: 'Fixture vault', status: 'ready'},
  capabilities: {bulk_checks: true, credentials: true, file_conflicts: true, files: true, host_routes: true, hosts: true, skills: true,
    credential_detach_delete: true, credential_probe: true, credential_test_history: true, local_key_browser: true,
    private_key_validation: true, ssh_agent_identities: true, native_dialogs: false, native_window: false,
    max_bulk_hosts: 64, max_check_concurrency: 4, max_web_transfer_bytes: 100 * 1024 * 1024},
};
const skill = {id: 'skill-one', name: 'production-operations', description: 'Deployment and maintenance instructions for the production hosts.',
  path: '/fixture/skills/production-operations/SKILL.md', status: 'ready', status_message: null, created_at: observed, updated_at: observed,
  servers: [{id: hosts[0].id, alias: hosts[0].alias}]};
const savedPath = (label: string, path: string, index: number) => ({id: `saved/#${index}`, server: hosts[0].alias, label, path,
  notes: '', created_at: observed, updated_at: observed, last_used_at: null as string | null});
const seedPaths = [savedPath('Application root', '/srv/app', 1), savedPath('Application logs', '/var/log/app', 2),
  savedPath('Deployments', '/opt/releases', 3), savedPath('Home', '/home/deploy', 4)];
const seedEntries = [
  {name: 'config', type: 'directory', size: 0}, {name: 'logs', type: 'directory', size: 0},
  {name: 'releases', type: 'directory', size: 0}, {name: 'current', type: 'symlink', size: 0, link_target: '/srv/app/releases/current'},
  {name: 'app.py', type: 'file', size: 2456}, {name: 'package.json', type: 'file', size: 1784},
  {name: 'README.md', type: 'file', size: 3800}, {name: 'backup.tar.gz', type: 'file', size: 150 * 1024 * 1024},
  {name: '.env.example', type: 'file', size: 310, hidden: true},
];
type RequestRecord = {method: string; path: string; body: string | null};

async function mockApi(page: Page, empty = false, unobserved = false) {
  const requests: RequestRecord[] = [], unexpected: string[] = [], errors: string[] = [];
  const inventory = hosts.map(value => ({...value, facts: unobserved ? null : {...value.facts, observed_at: new Date().toISOString()}}));
  let paths = seedPaths.map(value => ({...value}));
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/v1/**', async route => {
    const request = route.request(), url = new URL(request.url());
    const parts = url.pathname.slice('/api/v1/'.length).split('/').map(decodeURIComponent);
    const method = request.method();
    requests.push({method, path: url.pathname + url.search, body: request.postData()});
    const reply = async (value: unknown, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(value)});
    if (method !== 'GET' && request.headers()['x-csrf-token'] !== runtime.csrf) {
      unexpected.push(`Missing CSRF: ${method} ${url.pathname}`);
      return reply({error: 'csrf_failed', message: 'Fixture CSRF verification failed'}, 403);
    }
    if (method === 'GET' && parts[0] === 'runtime') return reply(runtime);
    if (method === 'GET' && parts[0] === 'hosts' && parts.length === 1) return reply(empty ? [] : inventory);
    if (method === 'GET' && parts[0] === 'credentials' && parts.length === 1) return reply(empty ? [] : [credential]);
    if (method === 'GET' && parts[0] === 'credentials' && parts[1] === credential.id && parts[2] === 'deletion-preview') {
      return reply({credential_id: credential.id, expected_updated_at: credential.updated_at,
        expected_host_ids: hosts.map(value => value.id).sort(), hosts: hosts.map(({id, alias}) => ({id, alias})),
        has_secret: false, has_passphrase: false});
    }
    if (method === 'GET' && parts[0] === 'credential-deletions') return reply([]);
    if (method === 'POST' && parts[0] === 'credentials' && parts[1] === credential.id && parts[2] === 'test') {
      const result = {ok: false, error_code: 'authentication-failed', message: 'The selected credential was not accepted.',
        latency_ms: 412, checked_at: observed, host_id: hosts[0].id, host_alias: hosts[0].alias,
        credential_id: credential.id, method: 'publickey', stages: []};
      const lines = [{type: 'stage', stage: 'authenticate', status: 'failed', message: result.message}, {type: 'result', result}]
        .map(event => JSON.stringify(event)).join('\n') + '\n';
      return route.fulfill({status: 200, contentType: 'application/x-ndjson', body: lines});
    }
    if (method === 'GET' && parts[0] === 'contexts') return reply(empty ? [] : [{name: 'production', count: hosts.length}]);
    if (method === 'GET' && parts[0] === 'skills') return reply(empty ? [] : [skill]);
    if (parts[0] === 'hosts') {
      const collect = (current: typeof inventory[number]) => {
        if (current.id === hosts[1].id) return {alias: current.alias, ok: false, latency_ms: 24, error_code: 'timeout', message: 'Remote system probe timed out'};
        current.facts = {...hosts.find(value => value.id === current.id)!.facts, observed_at: new Date().toISOString()};
        return {alias: current.alias, ok: true, latency_ms: 24, error_code: null, message: 'Host info collected'};
      };
      if (method === 'POST' && parts[1] === 'observe' && parts.length === 2) {
        const identifiers = request.postDataJSON().identifiers as string[];
        return reply({results: inventory.filter(value => identifiers.includes(value.id)).map(collect)});
      }
      const current = inventory.find(value => value.id === parts[1]);
      if (current && method === 'GET' && parts.length === 2) return reply(current);
      if (current && method === 'GET' && parts[2] === 'workspace') return reply({server: current, host: current, credential, paths, skills: [skill]});
      if (current && method === 'GET' && parts[2] === 'route') return reply({alias: current.alias, blocked_by: null,
        hops: [...current.proxy_jumps.map(alias => ({alias, external: true})), {alias: current.alias, external: false}]});
      if (current && method === 'POST' && parts[2] === 'test') return reply({alias: current.alias, ok: true,
        error_code: null, message: 'Connection checked', latency_ms: 24, checked_at: observed});
      if (current && method === 'POST' && parts[2] === 'diagnose') return reply({alias: current.alias, checks: []});
      if (current && method === 'POST' && parts[2] === 'observe') return reply(collect(current));
      if (current && method === 'GET' && parts[2] === 'files') {
        const path = url.searchParams.get('path') || '/srv/app', filter = url.searchParams.get('name_filter') || '';
        const entries = seedEntries.filter(value => value.name.toLowerCase().includes(filter.toLowerCase())).map(value => ({
          modified: 'Oct 15 12:00', modified_at: observed, permissions: value.type === 'directory' ? 'drwxr-xr-x' : '-rw-r--r--',
          owner: 'deploy', group: 'deploy', hidden: false, uid: 1001, gid: 1001, link_target: null,
          ...value, path: `${path === '/' ? '' : path}/${value.name}`,
        }));
        return reply({alias: current.alias, path, parent: path === '/' ? null : path.slice(0, path.lastIndexOf('/')) || '/',
          entries, total: seedEntries.length, matching_total: entries.length, truncated: false, unparsed: 0, latency_ms: 24, connection_checked_at: observed});
      }
      if (current && parts[2] === 'paths') {
        if (method === 'GET') return reply(paths);
        if (method === 'POST' && parts.length === 3) {
          const body = request.postDataJSON(); const saved = {...savedPath(body.label, body.path, 5), notes: body.notes || ''};
          paths = [...paths, saved]; return reply(saved, 201);
        }
        const saved = paths.find(value => value.id === parts[3]);
        if (saved && method === 'POST' && parts[4] === 'used') {saved.last_used_at = observed; return reply(saved);}
        if (saved && method === 'PATCH') {Object.assign(saved, request.postDataJSON()); return reply(saved);}
        if (saved && method === 'DELETE') {paths = paths.filter(value => value.id !== saved.id); return reply(paths);}
      }
      if (current && method === 'POST' && parts[2] === 'upload') {
        if (request.postData()?.includes('name="overwrite"\r\n\r\ntrue')) return reply({alias: current.alias, bytes: 13, path: '/srv/app/report.txt'});
        return reply({error: 'exists', message: 'Remote file already exists', existing: {type: 'file', size: 8, modified_at: observed}}, 409);
      }
      if (current && method === 'POST' && parts[2] === 'download') return route.fulfill({status: 200,
        headers: {'content-type': 'application/octet-stream', 'content-disposition': 'attachment; filename="README.md"'}, body: 'browser fixture'});
    }
    unexpected.push(`${method} ${url.pathname}`);
    return reply({error: 'fixture_unhandled', message: 'Unhandled browser fixture request'}, 500);
  });
  return {requests, unexpected, errors};
}

const nav = (page: Page, label: string) => page.getByRole('navigation', {name: 'Primary navigation'}).getByRole('link', {name: new RegExp(`^${label}(?:\\s*\\d+)?$`)});
const assertClean = (state: Awaited<ReturnType<typeof mockApi>>) => {expect(state.unexpected).toEqual([]); expect(state.errors).toEqual([]);};

test('hover lazily collects missing fields with inline skeletons and deduplicates revisits', async ({page}, info) => {
  const state = await mockApi(page, false, true);
  let release!: () => void;
  const responseGate = new Promise<void>(resolve => {release = resolve;});
  await page.route(/\/hosts\/[^/]+\/observe$/, async route => {await responseGate; await route.fallback();});
  await page.goto('/assets/');
  const name = page.getByRole('button', {name: hosts[0].alias, exact: true});
  await expect(name).toBeVisible();
  expect(state.requests.filter(request => request.method !== 'GET')).toEqual([]);
  await expect(page.getByRole('button', {name: /Collect host info/})).toHaveCount(0);
  await name.hover();
  await expect(page.getByLabel('Loading OS', {exact: true})).toBeVisible();
  await expect(page.getByLabel('Loading CPU load', {exact: true})).toBeVisible();
  await expect(page.getByRole('tooltip')).toContainText('Refreshing');
  await page.screenshot({path: info.outputPath('host-preview-loading.png')});
  release();
  await expect(page.getByRole('tooltip')).toContainText('Ubuntu 24.04 LTS');
  await expect(page.getByLabel('Loading OS', {exact: true})).toHaveCount(0);
  await page.getByRole('heading', {name: 'Hosts', exact: true}).hover();
  await expect(page.getByRole('tooltip')).toHaveCount(0);
  await name.hover();
  await expect(page.getByRole('tooltip')).toContainText('Ubuntu 24.04 LTS');
  expect(state.requests.filter(request => request.method === 'POST').map(request => request.path)).toEqual([`/api/v1/hosts/${encodeURIComponent(hosts[0].id)}/observe`]);
  await page.screenshot({path: info.outputPath('host-preview-loaded.png')});
  await page.getByRole('button', {name: hosts[1].alias, exact: true}).hover();
  await expect(page.getByRole('tooltip')).toContainText('Refresh unavailable');
  await expect(page.getByRole('tooltip').locator('.preview-skeleton')).toHaveCount(0);
  assertClean(state);
});

test('Paper route fixture keeps children beside their jump hosts in privacy mode', async ({page}, info) => {
  const state = await mockApi(page);
  const demo = (alias: string, logo: string, os: string, jumps: string[] = [], tag = 'Production') => ({
    ...host(alias, alias, jumps), address_scope: jumps.length ? 'private' : 'public', tags: [tag],
    facts: {...host(alias, alias).facts, os_logo: logo, os_pretty: os, observed_at: new Date().toISOString()},
  });
  const inventory = [
    demo('prod-web-01', 'ubuntu', 'Ubuntu 24.04', ['bastion']),
    demo('prod-db-01', 'rocky', 'Rocky Linux 9', ['bastion']),
    {...demo('bastion', 'debian', 'Debian 12'), jump_for_count: 4},
    {...demo('staging-api', 'ubuntu', 'Ubuntu 22.04', ['bastion'], 'Staging'), last_test_status: 'failed', last_test_error_code: 'auth-failed'},
    demo('staging-web', 'ubuntu', 'Ubuntu 22.04', ['bastion'], 'Staging'),
    demo('lab-gpu-01', 'ubuntu', 'Ubuntu 22.04', ['lab-gateway'], 'Lab'),
    {...demo('lab-gateway', 'alpine', 'Alpine 3.20', [], 'Lab'), jump_for_count: 2, last_test_status: 'failed', last_test_error_code: 'timed-out'},
    demo('lab-build', 'debian', 'Debian 12', ['lab-gateway'], 'Lab'),
    {...demo('nas-home', 'unknown', 'OS unknown'), facts: null, credential_id: null, credential_kind: null, credential_label: null, tags: [], last_test_at: null, last_test_status: null},
  ];
  await page.route('**/api/v1/hosts', route => route.fulfill({json: inventory}));
  await page.goto('/assets/');
  await page.getByRole('button', {name: 'Hide details', exact: true}).click();
  expect(await page.locator('.host-row').evaluateAll(nodes => nodes.map(node => node.getAttribute('data-host')))).toEqual([
    'bastion', 'prod-web-01', 'prod-db-01', 'staging-api', 'staging-web', 'lab-gateway', 'lab-gpu-01', 'lab-build', 'nas-home',
  ]);
  await expect(page.locator('.route-child')).toHaveCount(6);
  await expect(page.locator('.route-child.blocked')).toHaveCount(2);
  await expect(page.locator('.route-child.route-last')).toHaveCount(2);
  await page.getByRole('button', {name: 'prod-web-01', exact: true}).hover();
  await expect(page.getByRole('tooltip')).toHaveCount(0);
  await assertNoPageOverflow(page);
  await page.screenshot({path: info.outputPath('hosts-route-details-hidden-paper.png'), fullPage: true});
  expect(state.requests.filter(request => request.method !== 'GET')).toEqual([]);
  assertClean(state);
});

for (const width of [1440, 390]) {
test(`details hidden at ${width}px preserves Paper lanes and masks rather than removing metadata`, async ({page}, info) => {
  const state = await mockApi(page);
  await page.setViewportSize({width, height: 900});
  await page.goto('/assets/');
  const headers = page.getByRole('columnheader');
  await expect(headers).toHaveCount(5);
  const before = await headers.evaluateAll(nodes => nodes.map(node => ({label: node.textContent, width: node.getBoundingClientRect().width})));
  await page.getByRole('button', {name: 'Hide details', exact: true}).click();
  await expect(page.getByRole('button', {name: 'Details hidden', exact: true})).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByLabel('Endpoint hidden')).toHaveCount(hosts.length);
  await expect(page.getByLabel('Credential hidden')).toHaveCount(hosts.length);
  expect(await headers.evaluateAll(nodes => nodes.map(node => ({label: node.textContent, width: node.getBoundingClientRect().width})))).toEqual(before);
  for (const value of [hosts[0].hostname, hosts[0].username, hosts[0].notes, hosts[0].geo.city, credential.label]) await expect(page.locator('main')).not.toContainText(value);
  await expect(page.getByRole('button', {name: `Copy SSH command ${hosts[0].alias}`, exact: true})).toBeDisabled();
  await assertNoPageOverflow(page);
  await page.screenshot({path: info.outputPath(`01-hosts-details-hidden-${width}.png`), fullPage: true});
  expect(state.requests.filter(request => request.method !== 'GET')).toEqual([]);
  assertClean(state);
});
}

for (const width of [1440, 390]) {
test(`explicit host info backfill at ${width}px preserves cheap checks and history`, async ({page}, info) => {
  const state = await mockApi(page, false, true);
  await page.setViewportSize({width, height: 900});
  await page.goto('/assets/');
  const row = page.getByRole('row').filter({has: page.getByRole('button', {name: hosts[0].alias, exact: true})});
  await expect(row).toContainText('OS unknown');
  expect(state.requests.filter(request => request.method !== 'GET')).toEqual([]);
  await page.getByRole('button', {name: `Run check ${hosts[0].alias}`, exact: true}).click();
  await expect(page.getByRole('status').filter({hasText: 'Connection checked'})).toBeVisible();
  await expect(row).toContainText('OS unknown');
  await page.getByRole('checkbox', {name: `Select ${hosts[0].alias}`, exact: true}).check();
  await page.getByRole('checkbox', {name: `Select ${hosts[1].alias}`, exact: true}).check();
  await assertNoPageOverflow(page);
  await page.screenshot({path: info.outputPath(`host-info-selection-${width}.png`), fullPage: true});
  await page.getByRole('button', {name: 'Collect host info', exact: true}).click();
  const results = page.getByRole('dialog', {name: 'Host info results', exact: true});
  await expect(results).toContainText('Host info collected');
  await expect(results).toContainText('Remote system probe timed out');
  await results.getByRole('button', {name: 'Close', exact: true}).click();
  await expect(results).toHaveCount(0);
  await expect(row).toContainText('Ubuntu 24.04 LTS');
  const writes = state.requests.filter(request => request.method === 'POST');
  expect(writes.map(request => request.path)).toEqual([`/api/v1/hosts/${encodeURIComponent(hosts[0].id)}/test`, '/api/v1/hosts/observe']);
  expect(JSON.parse(writes[1].body!)).toEqual({identifiers: [hosts[0].id, hosts[1].id], timeout: 8, concurrency: 4});
  await page.goForward();
  await expect(results).toBeVisible();
  expect(state.requests.filter(request => request.method === 'POST')).toHaveLength(2);
  assertClean(state);
});
}

test('Hide details suppresses active, delayed and keyboard-focus previews', async ({page}) => {
  const state = await mockApi(page);
  await page.goto('/assets/');
  const name = page.getByRole('button', {name: hosts[0].alias, exact: true});
  await name.hover();
  await expect(page.getByRole('tooltip')).toBeVisible();
  await page.getByRole('button', {name: 'Hide details', exact: true}).click();
  await expect(page.getByRole('tooltip')).toHaveCount(0);
  await name.hover();
  await name.focus();
  await expect(name).not.toHaveAttribute('aria-describedby');
  await expect(page.getByRole('tooltip')).toHaveCount(0);
  await expect(page.locator('main')).not.toContainText(hosts[0].hostname);
  await expect(page.locator('main')).not.toContainText(credential.label);
  await page.getByRole('button', {name: 'Show details', exact: true}).click();
  await expect(page.getByRole('tooltip')).toHaveCount(0);
  expect(state.requests.filter(request => request.method !== 'GET')).toEqual([]);
  assertClean(state);
});

test('inventory shortcuts preserve button and navigation activation and survive filtering', async ({page}) => {
  const state = await mockApi(page);
  await page.goto('/assets/');
  await expect(page.getByRole('heading', {name: 'Hosts', exact: true})).toBeVisible();
  await page.keyboard.press('ArrowDown');
  const check = page.getByRole('button', {name: `Run check ${hosts[0].alias}`, exact: true});
  await check.focus();
  await check.press('Enter');
  await expect(page.getByRole('status')).toContainText('Connection checked');
  expect(state.requests.filter(request => request.method !== 'GET')).toHaveLength(1);
  await page.getByRole('heading', {name: 'Hosts', exact: true}).click();
  await page.keyboard.press('ArrowDown');
  await page.getByRole('textbox', {name: 'Search hosts', exact: true}).fill(hosts[0].alias);
  await page.getByRole('heading', {name: 'Hosts', exact: true}).click();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await nav(page, 'Tags').focus();
  await nav(page, 'Tags').press('Enter');
  await expect(page.getByRole('heading', {name: 'Tags', exact: true})).toBeVisible();
  assertClean(state);
});

test('saved diagnosis and edit row pins remain functional', async ({page}) => {
  await page.addInitScript(() => localStorage.setItem('ssm.host-pins', JSON.stringify(['diagnose', 'edit'])));
  const state = await mockApi(page);
  await page.goto('/assets/');
  await page.getByRole('button', {name: `Run full diagnosis ${hosts[0].alias}`, exact: true}).click();
  const diagnosis = page.getByRole('dialog', {name: `Diagnosis · ${hosts[0].alias}`, exact: true});
  await expect(diagnosis).toBeVisible();
  await diagnosis.getByRole('button', {name: 'Close', exact: true}).click();
  await page.getByRole('button', {name: `Edit host ${hosts[0].alias}`, exact: true}).click();
  await expect(page.getByRole('dialog', {name: `Edit ${hosts[0].alias}`, exact: true})).toBeVisible();
  expect(state.requests.filter(request => request.method !== 'GET').map(request => request.path))
    .toEqual([`/api/v1/hosts/${encodeURIComponent(hosts[0].id)}/diagnose`]);
  assertClean(state);
});

test('unbound credentials can be tested and timeout checkpoints stay unconfirmed', async ({page}, info) => {
  const state = await mockApi(page);
  const probes: unknown[] = [];
  await page.route('**/api/v1/hosts', route => route.fulfill({contentType: 'application/json', body: JSON.stringify(
    hosts.map(value => ({...value, credential_id: null, credential_kind: null, credential_label: null})),
  )}));
  await page.route(`**/api/v1/credentials/${credential.id}/test`, route => {
    expect(route.request().method()).toBe('POST');
    expect(route.request().headers()['x-csrf-token']).toBe(runtime.csrf);
    probes.push(route.request().postDataJSON());
    return route.fulfill({contentType: 'application/x-ndjson',
      body: JSON.stringify({type: 'result', result: {ok: false, error_code: 'timeout', message: 'Credential authentication exceeded its deadline.',
        latency_ms: 8000, checked_at: observed, host_id: hosts[0].id, host_alias: hosts[0].alias, credential_id: credential.id, method: null, stages: []}}) + '\n'});
  });
  await page.goto('/assets/#/credentials');
  const open = page.getByRole('button', {name: 'Test on host…', exact: true});
  await expect(open).toBeEnabled();
  await open.click();
  const dialog = page.getByRole('dialog', {name: `Test ${credential.label}`, exact: true});
  await expect(dialog.getByText('Not run yet', {exact: true})).toBeVisible();
  expect(probes).toEqual([]);
  await dialog.getByRole('button', {name: 'Run test', exact: true}).click();
  await expect(dialog.getByText('Credential authentication exceeded its deadline.', {exact: true})).toBeVisible();
  await expect(dialog.locator('.test-step.ok')).toHaveCount(0);
  await expect(dialog.getByText('Not confirmed', {exact: true})).toHaveCount(3);
  await expect(dialog.getByText(/rejected for user|ssh-copy-id/)).toHaveCount(0);
  await assertNoPageOverflow(page);
  await page.screenshot({path: info.outputPath('credential-timeout.png'), fullPage: true});
  expect(probes).toEqual([{host_id: hosts[0].id, timeout: 8}]);
  expect(state.requests.filter(request => request.method !== 'GET')).toEqual([]);
  assertClean(state);
});

async function assertNoPageOverflow(page: Page) {
  const bounds = await page.evaluate(() => ({width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
    overflowing: [...document.querySelectorAll<HTMLElement>('main,header,section,aside,form,.modal')]
      .filter(element => element.getBoundingClientRect().right > innerWidth + 1)
      .map(element => ({tag: element.tagName, class: element.className, right: Math.round(element.getBoundingClientRect().right)}))}));
  expect(bounds.scrollWidth, JSON.stringify(bounds.overflowing)).toBeLessThanOrEqual(bounds.width);
}

async function assertFocusTrap(page: Page, dialog: Locator) {
  const focusables = dialog.locator('button:not([disabled]),input:not([disabled]):not([type=hidden]),select:not([disabled]),textarea:not([disabled]),a[href],[tabindex="0"]').filter({visible: true});
  await focusables.last().focus();
  await page.keyboard.press('Tab');
  await expect(focusables.first()).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(focusables.last()).toBeFocused();
}

async function closeWithEscape(page: Page, dialog: Locator, launcher: Locator) {
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(launcher).toBeFocused();
  await expect.poll(() => page.evaluate(() => new URLSearchParams(location.hash.split('?')[1] || '').getAll('dialog').length)).toBe(0);
}

for (const width of [1440, 1024, 820, 640]) {
test(`all six views at ${width}px use the v1 contract and local host details stay read-only`, async ({page}, info) => {
  const state = await mockApi(page);
  await page.setViewportSize({width, height: 900});
  await page.goto('/assets/');
  await expect(page.getByRole('heading', {name: 'Hosts', exact: true})).toBeVisible();
  await assertNoPageOverflow(page);
  await page.screenshot({path: info.outputPath(`01-hosts-${width}.png`), fullPage: true});
  await page.getByRole('button', {name: 'prod-web-01', exact: true}).hover();
  await expect(page.getByRole('tooltip')).toBeVisible();
  await page.screenshot({path: info.outputPath(`01b-host-preview-${width}.png`), fullPage: false});
  await page.getByRole('button', {name: 'prod-web-01', exact: true}).click();
  const detail = page.getByRole('dialog', {name: 'Host details prod-web-01'});
  await expect(detail.getByRole('heading', {name: 'Route'})).toBeVisible();
  await expect(detail.getByText('SHA256:browser-fixture')).toBeVisible();
  await expect(detail.getByText('Application root')).toBeVisible();
  expect(state.requests.filter(request => request.method !== 'GET')).toEqual([]);
  expect(state.requests.some(request => /\/(?:test|observe|diagnose|upload|download)(?:\?|$)/.test(request.path))).toBe(false);
  await assertNoPageOverflow(page);
  await page.screenshot({path: info.outputPath(`02-host-details-${width}.png`), fullPage: true});
  await detail.locator('.drawer-body').evaluate(node => {node.scrollTop = node.scrollHeight;});
  await page.screenshot({path: info.outputPath(`02b-host-details-system-${width}.png`), fullPage: true});
  await detail.locator('.drawer-body').evaluate(node => {node.scrollTop = 0;});
  await assertFocusTrap(page, detail);
  await closeWithEscape(page, detail, page.getByRole('button', {name: 'prod-web-01', exact: true}));
  await page.goto('/assets/#/files');
  await expect(page.getByRole('table', {name: 'Remote files'})).toBeVisible();
  await expect(page.getByRole('button', {name: 'Download backup.tar.gz'})).toBeDisabled();
  await expect(page.getByRole('button', {name: 'Download current'})).toBeDisabled();
  await assertNoPageOverflow(page);
  await page.screenshot({path: info.outputPath(`03-files-${width}.png`), fullPage: true});
  await nav(page, 'Tags').click();
  await expect(page.getByRole('heading', {name: 'Tags', exact: true})).toBeVisible();
  await expect(page.getByRole('heading', {name: 'production', exact: true})).toBeVisible();
  await assertNoPageOverflow(page);
  await page.screenshot({path: info.outputPath(`04-tags-${width}.png`), fullPage: true});
  await nav(page, 'Skills').click();
  await expect(page.getByRole('heading', {name: 'Skill Library', exact: true})).toBeVisible();
  await expect(page.getByRole('heading', {name: 'production-operations', exact: true})).toBeVisible();
  await assertNoPageOverflow(page);
  await page.screenshot({path: info.outputPath(`05-skills-${width}.png`), fullPage: true});
  await nav(page, 'Credentials').click();
  await expect(page.getByRole('heading', {name: 'Credentials', exact: true})).toBeVisible();
  await expect(page.getByRole('heading', {name: 'Deployment key', exact: true})).toBeVisible();
  await assertNoPageOverflow(page);
  await page.screenshot({path: info.outputPath(`06-credentials-${width}.png`), fullPage: true});
  assertClean(state);
});

test(`key dialogs at ${width}px contain keyboard focus and close with Escape`, async ({page}, info) => {
  const state = await mockApi(page);
  await page.setViewportSize({width, height: 900});
  await page.goto('/assets/');
  await expect(page.getByRole('heading', {name: 'Hosts', exact: true})).toBeVisible();
  const inspect = async (name: string, launcher: Locator, title: string, ready?: () => Promise<void>) => {
    await launcher.click();
    const dialog = page.getByRole('dialog', {name: title, exact: true});
    await expect(dialog).toBeVisible();
    if (ready) await ready();
    await assertNoPageOverflow(page);
    expect(await page.locator('input[type=password]').evaluateAll(nodes => nodes.every(node => !(node as HTMLInputElement).value))).toBe(true);
    await page.screenshot({path: info.outputPath(`${name}-${width}.png`), fullPage: true});
    await assertFocusTrap(page, dialog);
    await closeWithEscape(page, dialog, launcher);
  };
  await inspect('07-add-host', page.getByRole('button', {name: 'Add host', exact: true}), 'Add host');
  await inspect('08-import-hosts', page.getByRole('button', {name: 'Import SSH config', exact: true}), 'Import OpenSSH configuration');
  await page.getByRole('button', {name: 'prod-web-01', exact: true}).click();
  const detail = page.getByRole('dialog', {name: 'Host details prod-web-01', exact: true});
  await expect(detail.getByText('Application root', {exact: true})).toBeVisible();
  await inspect('09-assign-skills', detail.getByRole('button', {name: 'Attach', exact: true}), 'Skills for prod-web-01',
    async () => {await expect(page.getByRole('dialog', {name: 'Skills for prod-web-01', exact: true}).getByRole('checkbox', {name: /^production-operations/})).toBeVisible();});
  await expect(detail).toBeVisible();
  await closeWithEscape(page, detail, page.getByRole('button', {name: 'prod-web-01', exact: true}));
  await page.goto('/assets/#/files');
  await expect(page.getByRole('table', {name: 'Remote files'})).toBeVisible();
  await inspect('10-file-host-switcher', page.getByRole('button', {name: 'Switch file host', exact: true}), 'Switch file host');
  await inspect('11-save-directory', page.getByRole('button', {name: '☆ Save path', exact: true}), 'Save path');
  await page.getByLabel('Upload file').setInputFiles({name: 'report.txt', mimeType: 'text/plain', buffer: Buffer.from('browser-smoke')});
  const conflict = page.getByRole('dialog', {name: 'File already exists', exact: true});
  await expect(conflict).toBeVisible();
  await assertNoPageOverflow(page);
  await page.screenshot({path: info.outputPath(`12-upload-conflict-${width}.png`), fullPage: true});
  await assertFocusTrap(page, conflict);
  await page.keyboard.press('Escape');
  await expect(conflict).toHaveCount(0);
  await nav(page, 'Tags').click();
  await expect(page.getByRole('heading', {name: 'production', exact: true})).toBeVisible();
  await inspect('13-new-tag', page.getByRole('button', {name: 'New tag', exact: true}), 'New tag');
  await nav(page, 'Skills').click();
  await expect(page.getByRole('heading', {name: 'production-operations', exact: true})).toBeVisible();
  await inspect('14-register-skill', page.getByRole('button', {name: 'Register skill', exact: true}), 'Register local skill');
  await nav(page, 'Credentials').click();
  await expect(page.getByRole('heading', {name: 'Deployment key', exact: true})).toBeVisible();
  await inspect('15-test-credential', page.getByRole('button', {name: 'Test on host…', exact: true}), 'Test Deployment key',
    async () => {
      const dialog = page.getByRole('dialog', {name: 'Test Deployment key', exact: true});
      await expect(dialog.getByText('TCP connect 203.0.113.4:22')).toBeVisible();
      await dialog.getByRole('button', {name: 'Run test', exact: true}).click();
      await expect(dialog.getByText('Failed after 0.4 s')).toBeVisible();
      await expect(dialog.getByText('Public key rejected for user “deploy”')).toBeVisible();
      await expect(dialog.getByText('ssh-copy-id -i /fixture/keys/deploy_ed25519.pub deploy@203.0.113.4')).toBeVisible();
    });
  await inspect('16-delete-credential', page.getByRole('button', {name: 'Delete', exact: true}), 'Delete “Deployment key”?',
    async () => {
      const dialog = page.getByRole('dialog', {name: 'Delete “Deployment key”?', exact: true});
      await expect(dialog.getByText(/3 hosts use this credential/)).toBeVisible();
      await dialog.getByRole('textbox', {name: 'Type the label to confirm'}).fill('Deployment key');
      await expect(dialog.getByRole('button', {name: 'Delete credential', exact: true})).toBeEnabled();
    });
  await inspect('17-new-credential', page.getByRole('button', {name: 'Add credential', exact: true}), 'Add credential',
    async () => {
      const dialog = page.getByRole('dialog', {name: 'Add credential', exact: true});
      await expect(dialog.getByRole('radio', {name: /Private key file/})).toBeChecked();
      await dialog.getByRole('textbox', {name: 'Label'}).fill('ci-runner');
    });
  expect(state.requests.filter(value => value.method !== 'GET')).toHaveLength(2);
  expect(state.requests.filter(value => value.method !== 'GET')[0].path).toMatch(/\/upload$/);
  expect(state.requests.filter(value => value.method !== 'GET')[1].path).toMatch(/\/test$/);
  assertClean(state);
});
}

test('linked remote paths, filtering and explicit overwrite preserve the original host', async ({page}) => {
  const state = await mockApi(page);
  const directory = '/srv/space #&?中';
  await page.goto(`/assets/#/files/${encodeURIComponent(hosts[0].id)}?path=${encodeURIComponent(directory)}`);
  await expect(page.getByRole('table', {name: 'Remote files'})).toBeVisible();
  expect(new URL(`http://fixture${state.requests.find(value => value.path.includes('/files'))!.path}`).searchParams.get('path')).toBe(directory);
  await page.getByRole('textbox', {name: 'Filter this folder'}).fill('README');
  await expect(page.getByRole('button', {name: 'README.md', exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: 'app.py', exact: true})).toHaveCount(0);
  expect(new URL(`http://fixture${state.requests.filter(value => value.path.includes('/files')).at(-1)!.path}`).searchParams.get('name_filter')).toBe('README');
  await page.getByLabel('Upload file').setInputFiles({name: 'report.txt', mimeType: 'text/plain', buffer: Buffer.from('browser-smoke')});
  const conflict = page.getByRole('dialog', {name: 'File already exists'});
  await expect(conflict).toBeVisible();
  await expect(conflict.getByText(`${directory}/report.txt`, {exact: true})).toBeVisible();
  expect(state.requests.filter(value => value.path.endsWith('/upload'))).toHaveLength(1);
  await conflict.getByRole('button', {name: 'Overwrite this file'}).click();
  await expect(conflict).toHaveCount(0);
  const uploads = state.requests.filter(value => value.path.endsWith('/upload'));
  expect(uploads).toHaveLength(2);
  expect(uploads.every(value => value.path === `/api/v1/hosts/${encodeURIComponent(hosts[0].id)}/upload`)).toBe(true);
  expect(uploads[0].body).toContain('name="overwrite"\r\n\r\nfalse');
  expect(uploads[1].body).toContain('name="overwrite"\r\n\r\ntrue');
  expect(uploads[1].body).toContain(`${directory}/report.txt`);
  assertClean(state);
});

test('saved directory CRUD reaches encoded APIs with CSRF', async ({page}) => {
  const state = await mockApi(page);
  await page.goto('/assets/#/files');
  await expect(page.getByRole('table', {name: 'Remote files'})).toBeVisible();
  await page.getByRole('button', {name: '☆ Save path'}).click();
  const editor = page.getByRole('dialog', {name: 'Save path', exact: true});
  await editor.getByRole('textbox', {name: 'Label', exact: true}).fill('Acceptance logs');
  await editor.getByRole('textbox', {name: 'Remote path', exact: true}).fill('/var/log/acceptance #&?中');
  await editor.getByRole('button', {name: 'Save path', exact: true}).click();
  await expect(editor).toHaveCount(0);
  await page.getByRole('button', {name: 'Edit saved path Acceptance logs'}).click();
  const edit = page.getByRole('dialog', {name: 'Edit saved path'});
  await edit.getByRole('textbox', {name: 'Label', exact: true}).fill('Acceptance archive');
  await edit.getByRole('button', {name: 'Save path', exact: true}).click();
  await expect(edit).toHaveCount(0);
  await page.getByRole('button', {name: /Acceptance archive \/var\/log\/acceptance/}).click();
  await expect.poll(() => state.requests.some(value => value.path.endsWith('/paths/saved%2F%235/used'))).toBe(true);
  await page.getByRole('button', {name: 'Delete saved path Acceptance archive'}).click();
  const confirm = page.getByRole('dialog', {name: 'Delete saved path'});
  await confirm.getByRole('button', {name: 'Delete saved path'}).click();
  await expect(confirm).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'Delete saved path Acceptance archive'})).toHaveCount(0);
  expect(state.requests.some(value => value.method === 'PATCH' && value.path.endsWith('/paths/saved%2F%235'))).toBe(true);
  expect(state.requests.some(value => value.method === 'DELETE' && value.path.endsWith('/paths/saved%2F%235'))).toBe(true);
  assertClean(state);
});

test('Files has collapsed and focus layouts without page overflow', async ({page}, info) => {
  const state = await mockApi(page);
  await page.setViewportSize({width: 1024, height: 900});
  await page.goto('/assets/#/files');
  await expect(page.getByRole('table', {name: 'Remote files'})).toBeVisible();
  await expect(page.getByRole('complementary', {name: 'Saved paths'})).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path: info.outputPath('files-1024-collapsed.png'), fullPage: true});
  await page.getByRole('button', {name: 'Toggle saved paths panel', exact: true}).click();
  await expect(page.getByRole('complementary', {name: 'Saved paths'})).toBeVisible();
  await page.getByRole('button', {name: 'Focus', exact: true}).click();
  await expect(page.getByRole('navigation', {name: 'Primary navigation'})).toBeHidden();
  await expect(page.getByRole('complementary', {name: 'Saved paths'})).toBeHidden();
  await page.screenshot({path: info.outputPath('files-1024-focus.png'), fullPage: true});
  await page.keyboard.press('Escape');
  await expect(page.getByRole('navigation', {name: 'Primary navigation'})).toBeVisible();
  await page.setViewportSize({width: 390, height: 844});
  await page.screenshot({path: info.outputPath('files-390.png'), fullPage: true});
  const mobile = await page.evaluate(() => ({width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
    overflowing: [...document.querySelectorAll<HTMLElement>('*')].filter(element => element.getBoundingClientRect().right > innerWidth && getComputedStyle(element).overflowX === 'visible').map(element => ({class: element.className, width: element.getBoundingClientRect().width})).slice(0, 12)}));
  expect(mobile.scrollWidth, JSON.stringify(mobile.overflowing)).toBeLessThanOrEqual(mobile.width);
  assertClean(state);
});

test('dialog Back and Forward never replay transfers and rapid reopening stays open', async ({page}) => {
  const state = await mockApi(page);
  await page.goto('/assets/#/files');
  await expect(page.getByRole('table', {name: 'Remote files'})).toBeVisible();
  await page.getByRole('button', {name: 'Switch file host'}).click();
  const switcher = page.getByRole('dialog', {name: 'Switch file host'});
  await expect(switcher).toBeVisible();
  await page.goBack(); await expect(switcher).toHaveCount(0);
  await page.goForward(); await expect(switcher).toBeVisible();
  expect(state.requests.filter(value => value.method !== 'GET')).toHaveLength(0);
  await switcher.getByRole('button', {name: 'Close', exact: true}).click();
  await page.getByRole('button', {name: 'New file tab'}).click();
  const newTab = page.getByRole('dialog', {name: 'Open a file tab'});
  await expect(newTab).toBeVisible();
  await page.keyboard.press('Escape'); await expect(newTab).toHaveCount(0);
  await page.getByLabel('Upload file').setInputFiles({name: 'back-forward.txt', mimeType: 'text/plain', buffer: Buffer.from('browser-smoke')});
  const conflict = page.getByRole('dialog', {name: 'File already exists'});
  await expect(conflict).toBeVisible();
  await page.goBack(); await expect(conflict).toHaveCount(0);
  await page.goForward(); await expect(conflict).toBeVisible();
  expect(state.requests.filter(value => value.path.endsWith('/upload'))).toHaveLength(1);
  await conflict.getByRole('button', {name: 'Cancel', exact: true}).click();
  await page.getByLabel('Upload file').setInputFiles({name: 'rapid-reopen.txt', mimeType: 'text/plain', buffer: Buffer.from('browser-smoke')});
  await expect(conflict).toBeVisible();
  await expect(conflict.getByText('/srv/app/rapid-reopen.txt', {exact: true})).toBeVisible();
  expect(state.requests.filter(value => value.path.endsWith('/upload'))).toHaveLength(2);
  assertClean(state);
});

test('empty workspace shows useful states across every primary page', async ({page}) => {
  const state = await mockApi(page, true);
  await page.goto('/assets/');
  await expect(page.getByRole('heading', {name: 'Your servers, in one place'})).toBeVisible();
  await page.goto('/assets/#/files');
  await expect(page.getByRole('heading', {name: 'No hosts yet', exact: true})).toBeVisible();
  for (const [label, heading] of [['Tags', 'Create your first tag'],
    ['Skills', 'Bring your host instructions together'], ['Credentials', 'Add your first identity']]) {
    await nav(page, label).click(); await expect(page.getByRole('heading', {name: heading, exact: true})).toBeVisible();
  }
  assertClean(state);
});
