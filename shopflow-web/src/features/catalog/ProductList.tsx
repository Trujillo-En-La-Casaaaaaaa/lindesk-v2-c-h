import { Link } from 'react-router-dom';
import { listProducts } from '../../api/client';
import { ErrorBanner } from '../../components/ErrorBanner';
import { useAsyncResource } from '../../hooks/useAsyncResource';
import { formatMoney } from '../../lib/format';

/**
 * Catalog screen: `GET /api/products`. Availability comes from the gateway
 * (`inStock`); when a product is unavailable its order action is disabled.
 */
export function ProductList() {
  const { data, error, loading, reload } = useAsyncResource(() => listProducts(), []);
  const products = data?.items ?? [];

  return (
    <section className="screen" aria-labelledby="catalog-heading">
      <div className="screen__header">
        <h1 id="catalog-heading">Catalog</h1>
        <button type="button" className="button" onClick={reload} disabled={loading} data-testid="refresh-catalog">
          Refresh
        </button>
      </div>

      {error !== null && <ErrorBanner error={error} onRetry={reload} />}

      {loading && products.length === 0 && (
        <p className="state" role="status">
          Loading products…
        </p>
      )}

      {!loading && error === null && products.length === 0 && (
        <p className="state" role="status" data-testid="catalog-empty">
          No products are available yet.
        </p>
      )}

      {products.length > 0 && (
        <>
          <table className="table" data-testid="product-table">
            <caption>Products ({products.length})</caption>
            <thead>
              <tr>
                <th scope="col">Product</th>
                <th scope="col">SKU</th>
                <th scope="col">Price</th>
                <th scope="col">Availability</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {products.map((product) => (
                <tr key={product.productId} data-testid={`product-row-${product.productId}`}>
                  <th scope="row">
                    <Link to={`/products/${encodeURIComponent(product.productId)}`}>{product.name}</Link>
                  </th>
                  <td>{product.sku}</td>
                  <td data-testid={`product-price-${product.productId}`}>
                    {formatMoney(product.price, product.currency)}
                  </td>
                  <td data-testid={`product-availability-${product.productId}`}>
                    {product.inStock ? `Available (${product.availableQuantity} in stock)` : 'Unavailable'}
                  </td>
                  <td>
                    {product.inStock ? (
                      <Link
                        className="button"
                        to={`/products/${encodeURIComponent(product.productId)}/order`}
                        data-testid={`order-product-${product.productId}`}
                      >
                        Order
                      </Link>
                    ) : (
                      <button
                        type="button"
                        className="button"
                        disabled
                        aria-disabled="true"
                        title="This product is out of stock"
                        data-testid={`order-product-${product.productId}`}
                      >
                        Order
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted" data-testid="catalog-count">
            Showing {products.length} of {data?.total ?? products.length} products.
          </p>
        </>
      )}
    </section>
  );
}

export default ProductList;
