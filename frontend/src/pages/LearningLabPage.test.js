import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

const vite = await createServer({
  configFile: './vite.config.ts',
  server: { middlewareMode: true },
  appType: 'custom',
});
const { default: LearningLabPage } = await vite.ssrLoadModule('/src/pages/LearningLabPage.tsx');
after(() => vite.close());

test('LearningLabPage renders initial dashboard structure accessibly', () => {
  const html = renderToStaticMarkup(React.createElement(LearningLabPage));
  assert.ok(html.includes('Learning Lab'));
  assert.ok(html.includes('Loading Learning Lab'));
});

test('LearningLabPage educational sections and classes are well-defined in component code', async () => {
  const fs = await import('node:fs/promises');
  const source = await fs.readFile('src/pages/LearningLabPage.tsx', 'utf-8');

  // Verify all 10 beginner-first learning experience elements are present in the template
  assert.ok(source.includes('analogy-card'), 'Real-world analogy card present');
  assert.ok(source.includes('Real-World Analogy'), 'Real-World Analogy heading present');
  assert.ok(source.includes('problem-card'), 'What is the Problem card present');
  assert.ok(source.includes('What is the Problem?'), 'What is the Problem heading present');
  assert.ok(source.includes('mission-card'), 'Your Mission card present');
  assert.ok(source.includes('Your Mission'), 'Your Mission heading present');
  assert.ok(source.includes('example-comparison-card'), 'Code demonstration comparison card present');
  assert.ok(source.includes('Before (Vulnerable / Anti-pattern)'), 'Before label present');
  assert.ok(source.includes('Expected Behavior / Defense'), 'Expected behavior label present');
  assert.ok(source.includes('Progressive Hints'), 'Progressive hints header present');
  assert.ok(source.includes('technical-explanation-card'), 'Technical deep dive details present');
  assert.ok(source.includes('Why Does This Work?'), 'Why Does This Work summary present');
  assert.ok(source.includes('Completion Criteria'), 'Completion criteria present');
  assert.ok(source.includes('editor-container'), 'Interactive code editor container present');
  assert.ok(source.includes('Truthful Static Verification'), 'Static verification notice present');
});
