// Browser state written by the previous product name may contain an unresolved
// request. Read it before permitting another send or launch.
function previousKey(key: string): string {
  return key.replace(/^onefoundry([.:])/, 'agentpod$1');
}

export function readSessionKey(key: string): string | null {
  const current = sessionStorage.getItem(key);
  if (current !== null) return current;
  const previous = sessionStorage.getItem(previousKey(key));
  if (previous !== null) sessionStorage.setItem(key, previous);
  return previous;
}

export function clearSessionKey(key: string): void {
  sessionStorage.removeItem(key);
  sessionStorage.removeItem(previousKey(key));
}

export function readLocalKey(key: string): string | null {
  const current = localStorage.getItem(key);
  if (current !== null) return current;
  const previous = localStorage.getItem(previousKey(key));
  if (previous !== null) localStorage.setItem(key, previous);
  return previous;
}

export function clearLocalKey(key: string): void {
  localStorage.removeItem(key);
  localStorage.removeItem(previousKey(key));
}
