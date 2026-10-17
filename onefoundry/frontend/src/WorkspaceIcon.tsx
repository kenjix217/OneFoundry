const paths: Record<string, string> = {
  home: 'm3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z',
  work: 'M4 5h16v15H4z M8 5V3h8v2 M8 10h8 M8 15h5',
  inbox: 'M4 4h16l2 12v4H2v-4z M2 16h6l2 3h4l2-3h6',
  team: 'M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8 M2 21v-3a7 7 0 0 1 14 0v3 M17 4a4 4 0 0 1 0 8 M19 15a6 6 0 0 1 3 6',
  knowledge: 'M3 4h7l2 2 2-2h7v16h-7l-2 2-2-2H3z M12 6v16',
  settings: 'M4 6h16 M4 12h16 M4 18h16 M8 3v6 M16 9v6 M10 15v6',
  integrations: 'M7 7h4v4H7z M13 13h4v4h-4z M11 9h5a3 3 0 0 1 3 3v1 M13 15H8a3 3 0 0 1-3-3v-1',
  arrow: 'M5 12h14 M13 6l6 6-6 6',
  spark: 'm12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3z',
};
export function WorkspaceIcon({ name }: { name: string }) {
  return <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name] ?? paths.work} /></svg>;
}
