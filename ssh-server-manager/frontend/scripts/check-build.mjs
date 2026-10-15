import {mkdtemp, readFile, readdir, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, join, relative, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import {gzipSync} from 'node:zlib';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const committedAssets = resolve(frontend, '../scripts/ssh_server_manager/assets/ui');
const arguments_ = process.argv.slice(2);
if (arguments_.some(argument => argument !== '--allow-uncommitted')) throw new Error('Only --allow-uncommitted is supported.');
const allowUncommitted = arguments_.includes('--allow-uncommitted');
const temporary = await mkdtemp(join(tmpdir(), 'ssm-ui-build-'));

async function filesIn(directory, prefix = '') {
  const entries = await readdir(directory, {withFileTypes: true});
  const result = [];
  for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
    const name = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isSymbolicLink()) throw new Error(`Build output must not contain symlinks: ${name}`);
    if (entry.isDirectory()) result.push(...await filesIn(join(directory, entry.name), name));
    else if (entry.isFile()) result.push(name);
    else throw new Error(`Unexpected build output: ${name}`);
  }
  return result;
}

try {
  const generation = spawnSync(process.execPath, [join(frontend, 'node_modules/vite/bin/vite.js'), 'build',
    '--outDir', temporary, '--emptyOutDir'], {cwd: frontend, stdio: 'inherit'});
  if (generation.error) throw generation.error;
  if (generation.status !== 0) throw new Error(`UI build failed (${generation.status ?? generation.signal}).`);
  const generatedNames = await filesIn(temporary);
  const generated = new Map(await Promise.all(generatedNames.map(async name => [name, await readFile(join(temporary, name))])));
  const manifestBytes = generated.get('asset-manifest.json');
  if (!manifestBytes || !generated.has('index.html')) throw new Error('Build must include index.html and asset-manifest.json.');
  const manifest = JSON.parse(manifestBytes.toString());
  if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) throw new Error('Invalid asset manifest.');
  const assertFile = name => {
    if (typeof name !== 'string' || name.includes('\\') || name.startsWith('/') || name.split('/').some(part => part === '..' || part === '.')) throw new Error(`Unsafe manifest asset: ${name}`);
    if (!generated.has(name)) throw new Error(`Manifest asset is missing: ${name}`);
    return name;
  };
  const initial = new Set(['index.html']);
  const visited = new Set();
  function visit(key, includeInitial) {
    const item = manifest[key];
    if (!item || typeof item !== 'object') throw new Error(`Manifest import is missing: ${key}`);
    assertFile(item.file);
    for (const name of [...item.css || [], ...item.assets || []]) assertFile(name);
    if (includeInitial) {
      initial.add(item.file);
      for (const name of item.css || []) initial.add(name);
    }
    const marker = `${includeInitial}:${key}`;
    if (visited.has(marker)) return;
    visited.add(marker);
    for (const dependency of item.imports || []) visit(dependency, includeInitial);
    for (const dependency of item.dynamicImports || []) visit(dependency, false);
  }
  const entries = Object.keys(manifest).filter(key => manifest[key].isEntry);
  if (entries.length !== 1 || !manifest['index.html']?.isEntry) throw new Error('Expected exactly one index.html entry in the asset manifest.');
  visit('index.html', true);
  const html = generated.get('index.html').toString();
  for (const reference of html.matchAll(/(?:src|href)="\/assets\/([^"]+)"/g)) assertFile(reference[1]);
  if (!html.includes(`/assets/${manifest['index.html'].file}`)) throw new Error('index.html does not load its manifest entry.');
  for (const key of Object.keys(manifest)) visit(key, false);
  if (generatedNames.some(name => name.endsWith('.map'))) throw new Error('Shipped build must not contain source maps.');
  const total = generatedNames.filter(name => /\.(?:js|css)$/.test(name) || name === 'index.html');
  const compressed = names => [...names].reduce((size, name) => size + gzipSync(generated.get(name), {level: 9}).length, 0);
  const initialBytes = compressed(initial), totalBytes = compressed(total);
  console.log(`UI gzip: initial ${(initialBytes / 1024).toFixed(1)} / 150 KiB; total ${(totalBytes / 1024).toFixed(1)} / 250 KiB.`);
  if (initialBytes > 150 * 1024 || totalBytes > 250 * 1024) throw new Error('UI exceeds its initial or total gzip budget.');
  const savedNames = await filesIn(committedAssets);
  const missing = generatedNames.filter(name => !savedNames.includes(name));
  const extra = savedNames.filter(name => !generated.has(name));
  const changed = [];
  for (const name of generatedNames.filter(name => savedNames.includes(name))) {
    if (!generated.get(name).equals(await readFile(join(committedAssets, name)))) changed.push(name);
  }
  if (missing.length || extra.length || changed.length) throw new Error(`Saved UI differs from a fresh build. Run npm run build. Missing: ${missing.join(', ') || 'none'}; extra: ${extra.join(', ') || 'none'}; changed: ${changed.join(', ') || 'none'}.`);
  const repository = spawnSync('git', ['rev-parse', '--show-toplevel'], {cwd: frontend, encoding: 'utf8'});
  if (repository.error) throw repository.error;
  if (repository.status !== 0) throw new Error('Cannot verify committed build output outside a Git checkout.');
  const root = repository.stdout.trim();
  const status = spawnSync('git', ['status', '--porcelain', '--untracked-files=all', '--', relative(root, committedAssets)], {cwd: root, encoding: 'utf8'});
  if (status.error) throw status.error;
  if (status.status !== 0) throw new Error('Cannot check build output against Git.');
  if (status.stdout.trim() && !allowUncommitted) throw new Error('UI build is reproducible, but its assets have uncommitted changes. Commit the assets, or use --allow-uncommitted for local validation.');
  console.log(`Saved UI matches a fresh build${status.stdout.trim() ? ' (local uncommitted assets allowed)' : ' and has no Git changes'}.`);
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
} finally {
  await rm(temporary, {recursive: true, force: true});
}
