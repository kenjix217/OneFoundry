import { readFileSync, readdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, expect, it } from 'vitest';
import { THEME_PRESETS } from '../src/themePresets';

const root = resolve(import.meta.dirname, '../src');
const styles = readdirSync(root).filter((name) => name.endsWith('.css'));
const source = (name: string) => readFileSync(resolve(root, name), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
const colorLiteral = /#[\da-f]{3,8}\b|\b(?:rgba?|hsla?|oklch|oklab|lab|lch)\(|(?:^|[\s:,(])(?:white|black)(?=[\s;!,)])/i;
const paletteAssignment = /--(?:theme-[\w-]+|canvas|surface(?:-soft)?|ink(?:-muted)?|line|sidebar(?:-deep)?|accent(?:-strong)?|error|status-[\w-]+|shadow-panel)\s*:/;

afterEach(() => { delete document.documentElement.dataset.uiTheme; });

it('gives the global palette sole ownership of colors, including fallbacks and overlays', () => {
  const offenders = styles.filter((name) => name !== 'theme-palettes.css').flatMap((name) =>
    source(name).split('\n').flatMap((line, index) => colorLiteral.test(line) || paletteAssignment.test(line) ? [`${name}:${index + 1}`] : []));
  expect(offenders, 'Page styles must consume global semantic colors, never define another palette').toEqual([]);
});

it('defines every semantic color used by any page for every selectable theme', () => {
  const palette = source('theme-palettes.css');
  const rules = [...palette.matchAll(/([^{}]+)\{([^{}]*)\}/g)];
  const assignments = (body: string) => [...body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)];
  const used = new Set(styles.flatMap((name) => [...source(name).matchAll(/var\((--[\w-]+)/g)].map((match) => match[1])));
  const semantic = [...used].filter((name) => paletteAssignment.test(`${name}:`));
  for (const { id } of THEME_PRESETS) {
    document.documentElement.dataset.uiTheme = id;
    const applicable = rules.filter(([, selector]) => document.documentElement.matches(selector.trim()));
    const values = new Map(applicable.flatMap(([, , body]) => assignments(body).map(([, name, value]) => [name, value] as const)));
    expect(semantic.filter((name) => !values.has(name)), `${id} is missing semantic colors`).toEqual([]);
    for (const [name, value] of values) {
      const missing = [...value.matchAll(/var\((--[\w-]+)/g)].map((match) => match[1]).filter((reference) => !values.has(reference));
      expect(missing, `${id}: ${name} has unresolved color references`).toEqual([]);
    }
  }
});

it('uses foreground and line theme roles for Integration Hub controls', () => {
  const hub = source('extensionHub.css');
  expect(hub).not.toMatch(/var\(--accent\)/);
  expect(hub).toMatch(/\.extension-hub-link\s*\{[^}]*color:\s*var\(--theme-link\)/);
  expect(hub).toMatch(/\.extension-hub-back\s*\{[^}]*color:\s*var\(--theme-link\)/);
  expect(hub).toMatch(/\.extension-hub-tabs button\[aria-current="page"\]\s*\{[^}]*border-bottom-color:\s*var\(--theme-accent-line\)/);
});
