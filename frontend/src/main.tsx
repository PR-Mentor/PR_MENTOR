import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import { AuthProvider } from './auth/AuthContext';
import './styles.css';
import './readability.css';

ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><BrowserRouter basename={import.meta.env.BASE_URL}><AuthProvider><App /></AuthProvider></BrowserRouter></React.StrictMode>);
