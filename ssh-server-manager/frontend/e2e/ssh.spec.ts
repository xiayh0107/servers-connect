import {test as base, expect, type Page} from '@playwright/test';
import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
import {mkdtemp, readFile, rm, access} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';

type Loopback = {base_url: string; hosts: Record<string, string>; max_web_transfer_bytes: number};

async function waitForReady(child: ChildProcessWithoutNullStreams, filename: string): Promise<Loopback> {
  const deadline = Date.now() + 40_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error('Isolated SSH/SFTP fixture exited before becoming ready');
    try {return JSON.parse(await readFile(filename, 'utf8')) as Loopback;} catch { /* Private file is still being created. */ }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('Isolated SSH/SFTP fixture startup timed out');
}

async function stopFixture(child: ChildProcessWithoutNullStreams) {
  if (child.exitCode !== null) return;
  const exited = new Promise<void>(resolve => child.once('exit', () => resolve()));
  child.stdin.end('stop\n'); // Graceful cleanup also works on Windows.
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_, reject) => {timer = setTimeout(() => {
    child.kill('SIGKILL'); reject(new Error('Isolated SSH/SFTP fixture failed to cleanly stop'));
  }, 15_000);});
  try {await Promise.race([exited, deadline]);} finally {if (timer) clearTimeout(timer);}
}

const test = base.extend<{loopback: Loopback}>({
  loopback: async ({page}, use) => {
    const directory = await mkdtemp(join(tmpdir(), 'ssm-browser-'));
    const launchFile = join(directory, 'launch-url'), readyFile = join(directory, 'ready.json');
    const project = resolve(process.cwd(), '..');
    const python = process.env.SSM_E2E_PYTHON || join(project, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
    const child = spawn(python, [join(project, 'tests/manual/modern_fixture.py'), '--url-file', launchFile,
      '--ready-file', readyFile, '--stdin-stop'], {cwd: project, stdio: ['pipe', 'pipe', 'pipe']});
    let startupError: Error | undefined;
    child.on('error', error => {startupError = error;});
    // Drain output; never attach login URLs, CSRF values or request bodies to artifacts.
    child.stdout.on('data', () => {}); child.stderr.on('data', () => {});
    try {
      if (startupError) throw new Error('Isolated SSH/SFTP fixture Python could not start');
      const fixture = await waitForReady(child, readyFile);
      const launchUrl = (await readFile(launchFile, 'utf8')).trim();
      try {await page.goto(launchUrl);} catch {throw new Error('Isolated fixture login failed');}
      await expect(page.getByRole('heading', {name: 'Hosts', exact: true})).toBeVisible();
      if (await page.evaluate(() => new URL(location.href).searchParams.has('token'))) {
        throw new Error('Launch URL was not removed after login');
      }
      let consumed = false;
      try {await access(launchFile);} catch {consumed = true;}
      expect(consumed, 'The one-use launch file is deleted after login').toBe(true);
      await use(fixture);
    } finally {
      await stopFixture(child);
      await rm(directory, {recursive: true, force: true});
    }
  },
});

async function files(page: Page, loopback: Loopback, alias: string, path = '/') {
  await page.goto(`${loopback.base_url}/#/files/${encodeURIComponent(loopback.hosts[alias])}?path=${encodeURIComponent(path)}`);
  await expect(page.getByRole('heading', {name: alias, exact: true})).toBeVisible();
  await expect(page.getByRole('table', {name: 'Remote files'})).toBeVisible();
  await expect(page.getByRole('button', {name: 'Upload here', exact: true})).toBeEnabled();
}

async function downloadText(page: Page, name: string) {
  const downloading = page.waitForEvent('download');
  await page.getByRole('button', {name: `Download ${name}`, exact: true}).click();
  const result = await downloading;
  expect(result.suggestedFilename()).toBe(name);
  const filename = await result.path();
  if (!filename) throw new Error('Fixture download did not complete');
  return readFile(filename, 'utf8');
}

test('real direct, multi-hop and external SSH checks, diagnosis and failure', async ({page, loopback}) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  for (const alias of ['direct', 'multi-hop', 'external-route', 'bad-auth']) {
    const checked = page.waitForResponse(response => response.url().endsWith(`/hosts/${loopback.hosts[alias]}/test`) && response.request().method() === 'POST');
    await page.getByRole('button', {name: `check ${alias}`, exact: true}).click();
    const result = await (await checked).json();
    expect(result.ok).toBe(alias !== 'bad-auth');
    if (alias === 'bad-auth') expect(result.error_code).toBe('authentication-failed');
    const row = page.getByRole('row').filter({has: page.getByRole('button', {name: alias, exact: true})});
    await expect(row).toContainText(alias === 'bad-auth' ? 'authentication failed' : 'OK');
  }
  await page.getByRole('button', {name: 'direct', exact: true}).click();
  const details = page.getByRole('dialog', {name: 'Host details direct', exact: true});
  await expect(details).toContainText('loopback');
  await expect(details).toContainText('SHA256:');
  await details.getByRole('button', {name: 'Full diagnosis', exact: true}).click();
  const diagnosis = page.getByRole('dialog', {name: 'Diagnosis · direct', exact: true});
  await expect(diagnosis).toContainText('SFTP');
  await expect(diagnosis).toContainText('Remote system responded');
  await diagnosis.getByRole('button', {name: 'Close', exact: true}).click();
  await expect(details).toContainText('Fixture Linux');
  await details.getByRole('button', {name: 'Close host details', exact: true}).click();
  await page.getByRole('checkbox', {name: 'Select direct', exact: true}).check();
  await page.getByRole('checkbox', {name: 'Select bad-auth', exact: true}).check();
  await page.getByRole('button', {name: 'Test selected', exact: true}).click();
  const batch = page.getByRole('dialog', {name: 'Batch check results', exact: true});
  await expect(batch).toContainText('direct');
  await expect(batch).toContainText('bad-auth');
  await expect(batch).toContainText('Failed');
  expect(errors).toEqual([]);
});

test('real routed SFTP browse, upload conflict, explicit overwrite and download', async ({page, loopback}) => {
  await files(page, loopback, 'multi-hop');
  const table = page.getByRole('table', {name: 'Remote files'});
  await expect(table).toContainText('readme.txt');
  await expect(table).toContainText('fixture');
  await page.getByRole('button', {name: 'readme.txt', exact: true}).click();
  await expect(page.getByLabel('Selected file metadata')).toContainText('/readme.txt');
  expect(await downloadText(page, 'readme.txt')).toBe('loopback SFTP download\n');
  await expect(table).not.toContainText('.hidden.txt');
  await page.getByRole('button', {name: /^Hidden files/}).click();
  await expect(table).toContainText('.hidden.txt');
  await page.getByLabel('Upload file').setInputFiles({name: 'existing.txt', mimeType: 'text/plain', buffer: Buffer.from('explicit browser replacement\n')});
  const conflict = page.getByRole('dialog', {name: 'File already exists', exact: true});
  await expect(conflict).toBeVisible();
  await expect(conflict).toContainText('multi-hop');
  await expect(conflict).toContainText('/existing.txt');
  await conflict.getByRole('button', {name: 'Cancel', exact: true}).click();
  await expect(conflict).not.toBeVisible();
  expect(await downloadText(page, 'existing.txt')).toBe('original fixture content\n');
  await page.getByLabel('Upload file').setInputFiles({name: 'existing.txt', mimeType: 'text/plain', buffer: Buffer.from('explicit browser replacement\n')});
  await expect(conflict).toBeVisible();
  await conflict.getByRole('button', {name: 'Overwrite this file', exact: true}).click();
  await expect(conflict).not.toBeVisible();
  await expect(page.getByRole('status').filter({hasText: 'Uploaded existing.txt'})).toBeVisible();
  expect(await downloadText(page, 'existing.txt')).toBe('explicit browser replacement\n');
  await page.getByLabel('Upload file').setInputFiles({name: 'new-browser.txt', mimeType: 'text/plain', buffer: Buffer.from('atomic no-overwrite upload\n')});
  await expect(table).toContainText('new-browser.txt');
  expect(await downloadText(page, 'new-browser.txt')).toBe('atomic no-overwrite upload\n');
  await page.getByRole('button', {name: 'Switch file host', exact: true}).click();
  const switcher = page.getByRole('dialog', {name: 'Switch file host', exact: true});
  await switcher.getByRole('button', {name: /external-route/}).click();
  await expect(page.getByRole('heading', {name: 'external-route', exact: true})).toBeVisible();
  await expect(table).toContainText('new-browser.txt');
});

test('independent credential authentication streams against a host with another saved key', async ({page, loopback}) => {
  const checked = page.waitForResponse(response => response.url().endsWith(`/hosts/${loopback.hosts['bad-auth']}/test`));
  await page.getByRole('button', {name: 'check bad-auth', exact: true}).click();
  expect((await (await checked).json()).ok).toBe(false);
  const before = await (await page.request.get(`${loopback.base_url}/api/v1/hosts/${loopback.hosts['bad-auth']}`)).json();
  expect(before.credential_id).toBe('fixture-wrong-key');
  await page.goto(`${loopback.base_url}/#/credentials/fixture-key`);
  await page.getByRole('button', {name: 'Test on host', exact: true}).click();
  const dialog = page.getByRole('dialog').filter({has: page.getByRole('heading', {name: 'Test credential · Loopback fixture key', exact: true})});
  await expect(dialog).toHaveAccessibleName('Test credential · Loopback fixture key');
  await dialog.getByRole('combobox', {name: 'Host', exact: true}).selectOption(loopback.hosts['bad-auth'], {timeout: 10_000});
  await dialog.getByRole('button', {name: 'Run test', exact: true}).click();
  await expect(dialog.getByText('Credential verified', {exact: true})).toBeVisible();
  await expect(dialog).toContainText('The selected credential was accepted.');
  const after = await (await page.request.get(`${loopback.base_url}/api/v1/hosts/${loopback.hosts['bad-auth']}`)).json();
  expect(after).toEqual(before);
  await dialog.locator('header').getByRole('button', {name: 'Close', exact: true}).click();
  const history = page.locator('.detail-panel .panel-section').filter({has: page.getByRole('heading', {name: 'Authentication history', exact: true})});
  await expect(history).toContainText('Credential verified');
  await expect(history).toContainText('bad-auth');
});

test('large directory full-name filtering, transfer caps, empty folder and nullable metadata', async ({page, loopback}) => {
  await files(page, loopback, 'direct', '/large');
  await expect(page.getByRole('status').filter({hasText: '5005 parsed entries'})).toBeVisible();
  await expect(page.getByText('Results are truncated.', {exact: false})).toBeVisible();
  await expect(page.getByRole('button', {name: /^Show next/})).toBeVisible();
  await page.getByLabel('Filter this folder').fill('entry-05004');
  await expect(page.getByRole('table', {name: 'Remote files'})).toContainText('entry-05004.txt');
  await expect(page.getByRole('status').filter({hasText: '1 matches · 5005 parsed entries'})).toBeVisible();
  await files(page, loopback, 'direct');
  await expect(page.getByRole('button', {name: 'Download oversize.bin', exact: true})).toBeDisabled();
  const capResult = await page.evaluate(async ({hostId}) => {
    const runtime = await (await fetch('/api/v1/runtime')).json();
    const response = await fetch(`/api/v1/hosts/${hostId}/download`, {method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': runtime.csrf}, body: JSON.stringify({path: '/oversize.bin'})});
    const body = await response.json(); return {status: response.status, error: body.error_code || body.error};
  }, {hostId: loopback.hosts.direct});
  expect(capResult).toEqual({status: 413, error: 'too_large'});
  await files(page, loopback, 'direct', '/empty');
  await expect(page.getByRole('heading', {name: 'This folder is empty', exact: true})).toBeVisible();
  await files(page, loopback, 'metadata-fallback');
  await expect(page.getByRole('table', {name: 'Remote files'})).toContainText('readme.txt');
  await expect(page.locator('main')).not.toContainText('Invalid Date');
  await expect(page.locator('main')).not.toContainText('undefined');
});
