import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const checkOnly = args.includes('--check');
const targets = args.filter((arg) => arg !== '--check');
if (targets.length > 1 || targets.some((arg) => arg.startsWith('--'))) {
  throw new Error('Usage: node apply-branding.mjs [upstream-directory] [--check]');
}
const upstream = resolve(targets[0] ?? resolve(here, '../upstream'));
const manifest = JSON.parse(readFileSync(resolve(here, 'manifest.json'), 'utf8'));
const pin = JSON.parse(readFileSync(resolve(here, '../UPSTREAM.json'), 'utf8'));
const patch = resolve(here, 'yfei.patch');
const hash = (value) => createHash('sha256').update(value).digest('hex');
const textHash = (value) => hash(value.toString('utf8').replace(/\r\n/g, '\n'));
const git = (...command) => execFileSync('git', ['-c', 'core.autocrlf=false', ...command], {
  cwd: upstream, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
});

if (manifest.upstreamCommit !== pin.commit || git('rev-parse', 'HEAD').trim() !== pin.commit) {
  throw new Error('Upstream commit mismatch; revalidate branding before upgrading. No files changed.');
}
if (hash(readFileSync(patch)) !== manifest.patchSha256) {
  throw new Error('Branding patch checksum mismatch. No files changed.');
}
if (hash(readFileSync(resolve(upstream, pin.httpApiReference))) !== pin.httpApiReferenceSha256) {
  throw new Error('Pinned API reference mismatch. No files changed.');
}

const currentHashes = manifest.files.map((file) => {
  if (!file.path || file.path.startsWith('/') || file.path.includes('\\') || file.path.split('/').some((part) => part === '..' || part === '.git')) {
    throw new Error('Invalid branding manifest path. No files changed.');
  }
  return textHash(readFileSync(resolve(upstream, file.path)));
});
if (manifest.files.every((file, index) => file.sha256 === currentHashes[index])) {
  console.log(`yFei branding already applied and verified (${manifest.files.length} files).`);
} else {
  if (manifest.files.some((file, index) => file.baseSha256 !== currentHashes[index])) {
    throw new Error('Branding targets contain local or partial changes; inspect them before applying. No files changed.');
  }
  git('apply', '--check', '--whitespace=error-all', patch);
  if (checkOnly) {
    console.log(`yFei branding patch is ready (${manifest.files.length} files); no files changed.`);
  } else {
    git('apply', '--whitespace=error-all', patch);
    for (const file of manifest.files) {
      if (textHash(readFileSync(resolve(upstream, file.path))) !== file.sha256) {
        throw new Error(`Post-apply verification failed: ${file.path}. Inspect the worktree; no rollback was attempted.`);
      }
    }
    console.log(`Applied and verified yFei branding (${manifest.files.length} files).`);
  }
}
