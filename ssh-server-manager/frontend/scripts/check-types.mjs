import {mkdtemp, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const temporary = await mkdtemp(join(tmpdir(), 'ssm-api-types-'));
try {
  const output = join(temporary, 'generated.ts');
  const generation = spawnSync(process.execPath, [join(frontend, 'node_modules/openapi-typescript/bin/cli.js'),
    resolve(frontend, '../contracts/api-v1.openapi.json'), '--output', output], {cwd: frontend, stdio: 'inherit'});
  if (generation.error) throw generation.error;
  if (generation.status !== 0) throw new Error(`Type generation failed (${generation.status ?? generation.signal}).`);
  const [expected, actual] = await Promise.all([readFile(output), readFile(join(frontend, 'src/generated.ts'))]);
  if (!expected.equals(actual)) throw new Error('src/generated.ts is stale. Run npm run typegen, then commit the updated contract and types.');
  console.log('API types match the saved OpenAPI contract.');
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
} finally {
  await rm(temporary, {recursive: true, force: true});
}
