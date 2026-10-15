import {createRoot} from 'react-dom/client';
import App from './App';
import './styles.css';

// The server has already exchanged the launch token for an HttpOnly cookie.
// Never retain it in history, component state, diagnostics, or screenshots.
if (location.search) history.replaceState(null, '', location.pathname + location.hash);
createRoot(document.getElementById('root')!).render(<App/>);
