import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './App';
import { completeGoogleRedirect } from './connectorApi';
import { applyThemePreset, readThemePreset } from './themePresets';
import './theme.css';
import './theme-palettes.css';
import './workWorkspace.css';
import './workspaceDesign.css';
import './chiefWorkspace.css';

applyThemePreset(readThemePreset());
// A Google sign-in returns to the app root with ?code and ?state; finish it before rendering.
void completeGoogleRedirect().catch(() => undefined);

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
