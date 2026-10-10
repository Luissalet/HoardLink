import React from 'react';
import {createRoot} from 'react-dom/client';
import '@fontsource/source-sans-3/latin-400.css';
import '@fontsource/source-sans-3/latin-600.css';
import '@fontsource/source-sans-3/latin-700.css';
import '@fontsource/cinzel/latin-600.css';
import App from './App';
import './style.css';
createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
