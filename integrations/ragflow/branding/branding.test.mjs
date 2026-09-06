import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '../../..');
const upstream = resolve(here, '../upstream');
const pin = JSON.parse(readFileSync(resolve(here, '../UPSTREAM.json'), 'utf8'));
const read = (path) => readFileSync(resolve(upstream, path), 'utf8');
const original = (path) => execFileSync('git', ['show', `${pin.commit}:${path}`], {
  cwd: upstream, maxBuffer: 24 * 1024 * 1024,
});
const hash = (value) => createHash('sha256').update(value).digest('hex');
const projectLogo = readFileSync(resolve(root, 'web/public/logo.png'));
const assets = [
  'web/public/logo.svg',
  'web/src/assets/logo-with-text.svg',
  'web/src/assets/svg/logo-with-text-white.svg',
  'web/src/assets/svg/next-login-bg.svg',
];

for (const path of assets) {
  test(`${path} embeds the exact project logo, without remote dependencies`, () => {
    const svg = read(path);
    assert.ok(/<title>yFei/.test(svg), `${path}: missing yFei title`);
    const images = [...svg.matchAll(/(?:xlink:)?href="data:image\/png;base64,([A-Za-z0-9+/=]+)"/g)];
    assert.ok(images.some((item) => hash(Buffer.from(item[1], 'base64')) === hash(projectLogo)));
    assert.ok(!/(?:xlink:)?href="https?:/.test(svg), `${path}: remote image`);
    assert.ok(!/RAGFlow|RAG- logos/.test(svg), `${path}: old brand`);
  });
}

test('login background retains its original artwork but not the eight old brand paths', () => {
  const path = 'web/src/assets/svg/next-login-bg.svg';
  const before = original(path).toString('utf8');
  const after = read(path);
  const background = /<image id="image0_994_34945"[^>]+\/>/;
  assert.equal(hash(after.match(background)?.[0] ?? ''), hash(before.match(background)?.[0] ?? ''));
  const oldPaths = before.match(/^<path d="[^\n]+fill="white"\/>$/gm);
  assert.equal(oldPaths.length, 8);
  for (const oldPath of oldPaths) assert.ok(!after.includes(oldPath));
});

test('source pin, license, API reference and runtime configuration stay unchanged', () => {
  assert.equal(execFileSync('git', ['rev-parse', 'HEAD'], { cwd: upstream, encoding: 'utf8' }).trim(), pin.commit);
  assert.equal(hash(readFileSync(resolve(upstream, pin.httpApiReference))), pin.httpApiReferenceSha256);
  for (const path of ['LICENSE', 'go.mod', 'go.sum', 'uv.lock', 'docker/.env', 'docker/docker-compose.yml', 'docker/docker-compose-base.yml']) {
    assert.equal(hash(readFileSync(resolve(upstream, path))), hash(original(path)), path);
  }
});

test('third-party provider logos are preserved', () => {
  const paths = execFileSync('git', ['ls-files', 'web/src/assets/svg/llm'], { cwd: upstream, encoding: 'utf8' }).trim().split('\n');
  for (const path of paths) assert.equal(hash(readFileSync(resolve(upstream, path))), hash(original(path)), path);
});

test('frontend title and configuration use yFei', () => {
  assert.match(read('web/index.html'), /<title>yFei<\/title>/);
  assert.match(read('web/src/conf.json'), /"appName":\s*"yFei"/);
  assert.match(read('web/package.json'), /"description":\s*"yFei/);
});

test('shell launch and migration scripts change only visible brand strings', () => {
  for (const path of ['docker/entrypoint.sh', 'docker/launch_backend_service.sh', 'docker/migration.sh']) {
    const before = original(path).toString('utf8');
    const expected = before.split('\n').map((line) => /^\s*(?:echo|run_with_restart)\b/.test(line)
      ? line.replace(/RAGFlow/g, 'yFei') : line).join('\n');
    assert.equal(hash(read(path).replace(/\r\n/g, '\n')), hash(expected.replace(/\r\n/g, '\n')), path);
  }
});

test('changed frontend sources parse and have no old visible brand literals', () => {
  const require = createRequire(resolve(root, 'web/package.json'));
  const ts = require('typescript');
  const paths = execFileSync('git', ['-c', 'core.autocrlf=false', 'diff', '--name-only', '--', 'web'], { cwd: upstream, encoding: 'utf8' })
    .trim().split('\n').filter((path) => /\.[jt]sx?$/.test(path));
  assert.ok(paths.length > 0);
  for (const path of paths) {
    const source = ts.createSourceFile(path, read(path), ts.ScriptTarget.Latest, true);
    assert.equal(source.parseDiagnostics.length, 0, `${path}: syntax error`);
    function visit(node) {
      if (ts.isStringLiteralLike(node) || ts.isJsxText(node)) {
        const display = node.text.replace(/https?:\/\/[^\s"'<>]+/g, '').replace(/`[^`]*`/g, '');
        assert.doesNotMatch(display, /\b(?:RAGFlow|Ragflow|RAGFLOW)\b/, `${path}:${source.getLineAndCharacterOfPosition(node.pos).line + 1}`);
      }
      ts.forEachChild(node, visit);
    }
    visit(source);
  }
});

test('saved branding patch reproduces every file in a temporary index without staging user changes', () => {
  const manifest = JSON.parse(readFileSync(resolve(here, 'manifest.json'), 'utf8'));
  const patch = resolve(here, 'yfei.patch');
  assert.equal(manifest.upstreamCommit, pin.commit);
  assert.equal(manifest.projectLogoSha256, hash(projectLogo));
  assert.equal(manifest.patchSha256, hash(readFileSync(patch)));
  const directory = mkdtempSync(resolve(root, 'web/output/playwright/yfei-branding-index-'));
  const environment = { ...process.env, GIT_INDEX_FILE: resolve(directory, 'index') };
  const git = (...args) => execFileSync('git', ['-c', 'core.autocrlf=false', ...args], {
    cwd: upstream, env: environment, maxBuffer: 24 * 1024 * 1024,
  });
  git('read-tree', pin.commit);
  git('apply', '--cached', '--check', '--whitespace=error-all', patch);
  git('apply', '--cached', '--whitespace=error-all', patch);
  const textHash = (contents) => hash(contents.toString('utf8').replace(/\r\n/g, '\n'));
  assert.ok(manifest.files.length > 0);
  for (const file of manifest.files) {
    assert.equal(textHash(original(file.path)), file.baseSha256, `${file.path}: wrong base hash`);
    assert.equal(textHash(read(file.path)), file.sha256, `${file.path}: working copy not saved in patch`);
    assert.equal(textHash(git('show', `:${file.path}`)), file.sha256, `${file.path}: patch replay mismatch`);
  }
  assert.equal(hash(git('show', `:${pin.httpApiReference}`)), pin.httpApiReferenceSha256);
});

test('applying the saved patch again is a verified no-op', () => {
  const output = execFileSync(process.execPath, [resolve(here, 'apply-branding.mjs'), '--check'], {
    encoding: 'utf8', maxBuffer: 1024 * 1024,
  });
  assert.match(output, /already applied and verified/);
});

test('Git preserves the published patch line endings and checksum on Windows', () => {
  const attributes = execFileSync('git', ['check-attr', 'eol', '--', 'integrations/ragflow/branding/yfei.patch'], {
    cwd: root, encoding: 'utf8',
  });
  assert.match(attributes, /: eol: lf/);
});

test('replay helper applies to a fresh checkout and refuses to overwrite a local edit', () => {
  const manifest = JSON.parse(readFileSync(resolve(here, 'manifest.json'), 'utf8'));
  const directory = mkdtempSync(resolve(root, 'web/output/playwright/yfei-branding-replay-'));
  const fixture = resolve(directory, 'source');
  execFileSync('git', ['clone', '--quiet', '--shared', '--no-checkout', upstream, fixture], { stdio: 'pipe' });
  const options = { cwd: fixture, stdio: 'pipe' };
  execFileSync('git', ['read-tree', pin.commit], options);
  const paths = [...manifest.files.map((file) => file.path), pin.httpApiReference];
  execFileSync('git', ['-c', 'core.autocrlf=false', 'checkout-index', '--stdin'], {
    ...options, input: [...new Set(paths)].join('\n') + '\n',
  });
  const run = (...args) => execFileSync(process.execPath, [resolve(here, 'apply-branding.mjs'), fixture, ...args], {
    encoding: 'utf8', stdio: 'pipe',
  });
  assert.match(run('--check'), /patch is ready/);
  assert.match(run(), /Applied and verified/);
  assert.match(run(), /already applied and verified/);
  const localEdit = resolve(fixture, 'web/src/conf.json');
  const editedContent = readFileSync(localEdit, 'utf8') + '\n';
  writeFileSync(localEdit, editedContent);
  assert.throws(() => run(), (error) => {
    assert.equal(error.status, 1);
    assert.match(error.stderr.toString(), /contain local or partial changes/);
    return true;
  });
  assert.equal(readFileSync(localEdit, 'utf8'), editedContent);
});
