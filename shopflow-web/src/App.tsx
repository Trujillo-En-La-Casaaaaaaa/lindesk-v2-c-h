import { Link, NavLink, Route, Routes } from 'react-router-dom';
import { ProductList } from './features/catalog/ProductList';
import { ProductDetail } from './features/catalog/ProductDetail';
import { CreateOrder } from './features/orders/CreateOrder';
import { OrderStatus } from './features/orders/OrderStatus';
import { AdminOrders } from './features/admin/AdminOrders';

/**
 * Application shell: layout, navigation and the client side routes of the
 * single page application. Screens own their own gateway calls.
 */
export function App() {
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main">
        Skip to main content
      </a>

      <header className="app-shell__header">
        <Link className="app-shell__brand" to="/">
          ShopFlow
        </Link>
        <nav className="app-shell__nav" aria-label="Main">
          <NavLink to="/" end>
            Catalog
          </NavLink>
          <NavLink to="/admin/orders">Admin orders</NavLink>
        </nav>
      </header>

      <main id="main" className="app-shell__main" tabIndex={-1}>
        <Routes>
          <Route path="/" element={<ProductList />} />
          <Route path="/products/:productId" element={<ProductDetail />} />
          <Route path="/products/:productId/order" element={<CreateOrder />} />
          <Route path="/orders/:orderId" element={<OrderStatus />} />
          <Route path="/admin/orders" element={<AdminOrders />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>

      <footer className="app-shell__footer">
        <p>ShopFlow storefront - every request goes to the ShopFlow gateway.</p>
      </footer>
    </div>
  );
}

function NotFound() {
  return (
    <section className="screen" aria-labelledby="not-found-heading">
      <h1 id="not-found-heading">Page not found</h1>
      <p data-testid="not-found">This screen does not exist in ShopFlow.</p>
      <p>
        <Link to="/">Back to the catalog</Link>
      </p>
    </section>
  );
}

export default App;
