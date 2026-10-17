import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import ts from 'typescript';
import { expect, it } from 'vitest';
import { approvalPresentation } from '../src/approvalPresentation';
import type { Approval } from '../src/workApi';

it('keeps every application module reachable after retiring the legacy UI', () => {
  const root = resolve(import.meta.dirname, '../src');
  const files = readdirSync(root).filter((name) => /\.tsx?$/.test(name) && !name.endsWith('.d.ts')).map((name) => resolve(root, name));
  const visited = new Set<string>();
  function visit(file: string) {
    if (visited.has(file)) return;
    visited.add(file);
    const source = ts.createSourceFile(file, readFileSync(file, 'utf8'), ts.ScriptTarget.Latest, true);
    source.forEachChild((node) => {
      if ((!ts.isImportDeclaration(node) && !ts.isExportDeclaration(node)) || !node.moduleSpecifier || !ts.isStringLiteral(node.moduleSpecifier)) return;
      const path = node.moduleSpecifier.text;
      if (!path.startsWith('.')) return;
      const found = files.find((candidate) => candidate === resolve(dirname(file), `${path}.ts`) || candidate === resolve(dirname(file), `${path}.tsx`));
      if (found) visit(found);
    });
  }
  visit(resolve(root, 'main.tsx'));
  expect(files.filter((file) => !visited.has(file))).toEqual([]);
  expect(existsSync(resolve(root, 'ToolsPluginsView.tsx'))).toBe(false);
  expect(readFileSync(resolve(root, 'theme.css'), 'utf8')).not.toMatch(/\.tools-card|\.hero-motif|\.view-nav/);
});

it('presents system proposals without hashes while keeping outbound text exact', () => {
  const hash = 'a'.repeat(40);
  const base: Approval = { id: 'approval-1', project_id: 'project-1', run_id: 'run-1', work_order_id: 'order-1',
    action_type: 'other', target: `studio/app:main@${hash}`, content: JSON.stringify({ owner: 'studio', repo: 'app', branch: 'main', saved_revision: hash, source_id: 'source-1' }),
    proposal_digest: 'digest', status: 'pending', created_at: '2026-10-03T12:00:00Z', decided_at: null, synthetic_merge: false, github_stage: true };
  const shown = approvalPresentation(base);
  expect(shown.target).toBe('studio/app:main@saved version');
  expect(shown.content).toContain('Branch: main');
  expect(shown.content).not.toContain(hash);
  expect(shown.content).not.toContain('source-1');
  const exact = `Please review commit ${hash}.`;
  expect(approvalPresentation({ ...base, github_stage: false, github_comment: true, content: JSON.stringify({ comment: exact }) }).content).toBe(exact);
  expect(approvalPresentation({ ...base, github_stage: false, content: exact }).content).toBe(exact);
  expect(approvalPresentation({ ...base, content: '{bad json' }).content).toBe('{bad json');
});

it('does not render internal identifiers or hashes as interface metadata', () => {
  const root = resolve(import.meta.dirname, '../src');
  const forbidden = /^(id|sha256|proposal_digest|binding_digest|receipt_sha256|source_tree|request_id|provider_response_id|provider_session_id|actor_id|work_order_id|run_id|source_id)$/;
  const found: string[] = [];
  function renderedValue(node: ts.Expression): string[] {
    if (ts.isPropertyAccessExpression(node)) return forbidden.test(node.name.text) || node.getText() === 'artifact.path' ? [node.getText()] : [];
    if (ts.isConditionalExpression(node)) return [...renderedValue(node.whenTrue), ...renderedValue(node.whenFalse)];
    if (ts.isTemplateExpression(node)) return node.templateSpans.flatMap((span) => renderedValue(span.expression));
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression) && ['slice', 'substring', 'toString'].includes(node.expression.name.text)) return renderedValue(node.expression.expression);
    return [];
  }
  for (const file of readdirSync(root).filter((name) => name.endsWith('.tsx'))) {
    const source = ts.createSourceFile(file, readFileSync(resolve(root, file), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    function visit(node: ts.Node) {
      if (ts.isJsxExpression(node) && node.expression && !ts.isJsxAttribute(node.parent)) found.push(...renderedValue(node.expression).map((value) => `${file}: ${value}`));
      ts.forEachChild(node, visit);
    }
    visit(source);
  }
  expect(found).toEqual([]);
});
