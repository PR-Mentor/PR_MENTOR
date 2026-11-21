import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { createServer } from 'vite';

const vite = await createServer({ configFile: './vite.config.ts', server: { middlewareMode: true, ws: false }, appType: 'custom' });
const { filterRepositories } = await vite.ssrLoadModule('/src/services/repository-view.ts');
after(() => vite.close());

const repositories = [
  { name: 'secret-tools', full_name: 'DataOrg/secret-tools', visibility: 'private', language: 'Python', updated_at: '2025-02-01' },
  { name: 'apis', full_name: 'DataOrg/apis', visibility: 'internal', language: 'Python', updated_at: '2025-01-03' },
  { name: 'dashboard', full_name: 'alice/dashboard', visibility: 'public', language: 'TypeScript', updated_at: '2025-01-02' },
  { name: 'public-tools', full_name: 'DataOrg/public-tools', visibility: 'public', language: 'Python', updated_at: '2025-01-01' },
];

function names(options = {}) {
  return filterRepositories(repositories, { query: '', visibility: 'all', language: '', order: 'updated', ...options }).map(({ name }) => name);
}

test('searches across repository and owner names case-insensitively', () => {
  assert.deepEqual(names({ query: ' DataOrg/SECRET ' }), ['secret-tools']);
  assert.deepEqual(names({ query: 'TOOLS' }), ['secret-tools', 'public-tools']);
});

test('combines search, visibility, and language filters', () => {
  assert.deepEqual(names({ visibility: 'private', language: 'Python' }), ['secret-tools', 'apis']);
  assert.deepEqual(names({ visibility: 'public', query: 'tools', language: 'Python' }), ['public-tools']);
  assert.deepEqual(names({ visibility: 'private', language: 'TypeScript' }), []);
});

test('sorts without changing the original repository order', () => {
  assert.deepEqual(names({ order: 'name' }), ['apis', 'dashboard', 'public-tools', 'secret-tools']);
  assert.deepEqual(names(), ['secret-tools', 'apis', 'dashboard', 'public-tools']);
  assert.equal(repositories[0].name, 'secret-tools');
});
