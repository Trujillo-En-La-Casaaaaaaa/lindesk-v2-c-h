import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { App } from './App';
import './styles.css';

const container = document.getElementById('root');
if (!container) {
  throw new Error('ShopFlow failed to start: #root was not found in index.html');
}

createRoot(container).render(
  <BrowserRouter>
    <App />
  </BrowserRouter>,
);
