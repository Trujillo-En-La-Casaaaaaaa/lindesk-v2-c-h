import { Link, useParams } from 'react-router-dom';
import { getProduct } from '../../api/client';
import { ErrorBanner } from '../../components/ErrorBanner';
import { hasErrorCode } from '../../api/errors';
import { useAsyncResource } from '../../hooks/useAsyncResource';
import { formatMoney } from '../../lib/format';

/**
 * Product detail screen: `GET /api/products/{productId}`. It shows exactly what
 * the gateway reports and only offers an order entry point for in-stock
 * products.
 */
export function ProductDetail() {
  const { productId = '' } = useParams<{ productId: string }>();
  const { data: product, error, loading, reload } = useAsyncResource(() => getProduct(productId), [productId]);

  return (
    <section className="screen" aria-labelledby="product-heading">
      <nav className="breadcrumb" aria-label="Breadcrumb">
        <Link to="/">Back to catalog</Link>
      </nav>

      {error !== null && (
        <>
          <h1 id="product-heading">Product</h1>
          {hasErrorCode(error, 'not_found') && (
            <p className="state" role="status" data-testid="product-not-found">
              This product could not be found. It may have been removed.
            </p>
          )}
          <ErrorBanner error={error} onRetry={reload} />
        </>
      )}

      {loading && product === null && (
        <p className="state" role="status">
          Loading product…
        </p>
      )}

      {product !== null && (
        <>
          <h1 id="product-heading" data-testid="product-name">
            {product.name}
          </h1>
          <p className="muted" data-testid="product-sku">
            SKU {product.sku}
          </p>
          <p data-testid="product-description">
            {product.description.trim() === '' ? 'No description provided.' : product.description}
          </p>

          <dl className="facts">
            <div>
              <dt>Price</dt>
              <dd data-testid="product-price">{formatMoney(product.price, product.currency)}</dd>
            </div>
            <div>
              <dt>Available quantity</dt>
              <dd data-testid="product-available-quantity">{product.availableQuantity}</dd>
            </div>
            <div>
              <dt>Availability</dt>
              <dd data-testid="product-availability">{product.inStock ? 'Available' : 'Unavailable'}</dd>
            </div>
          </dl>

          {product.inStock ? (
            <p>
              <Link
                className="button"
                to={`/products/${encodeURIComponent(product.productId)}/order`}
                data-testid="create-order-link"
              >
                Create order
              </Link>
            </p>
          ) : (
            <>
              <p>
                <button
                  type="button"
                  className="button"
                  disabled
                  aria-disabled="true"
                  title="This product is out of stock"
                  data-testid="create-order-link"
                >
                  Create order
                </button>
              </p>
              <p className="muted" data-testid="unavailable-notice">
                This product is out of stock, so no order can be created right now.
              </p>
            </>
          )}
        </>
      )}
    </section>
  );
}

export default ProductDetail;
