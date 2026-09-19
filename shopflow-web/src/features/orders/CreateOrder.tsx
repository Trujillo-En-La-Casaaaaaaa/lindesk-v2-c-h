import { useState } from 'react';
import type { FormEvent } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { createOrder, getProduct } from '../../api/client';
import { ErrorBanner } from '../../components/ErrorBanner';
import {
  extractFieldErrors,
  hasErrorCode,
  insufficientStockMessage,
  toApiError,
} from '../../api/errors';
import type { ApiError, FieldErrors } from '../../api/errors';
import { useAsyncResource } from '../../hooks/useAsyncResource';
import { formatMoney } from '../../lib/format';

const DEFAULT_QUANTITY = '1';

/**
 * Order creation screen: collects `customerId` and `quantity` for one product
 * and calls `POST /api/orders` through the gateway. Stock limits are decided by
 * the services - the form only blocks obviously invalid input and mirrors the
 * gateway error codes.
 */
export function CreateOrder() {
  const { productId = '' } = useParams<{ productId: string }>();
  const navigate = useNavigate();

  const {
    data: product,
    error: productError,
    loading: productLoading,
    reload: reloadProduct,
  } = useAsyncResource(() => getProduct(productId), [productId]);

  const [customerId, setCustomerId] = useState('');
  const [quantity, setQuantity] = useState(DEFAULT_QUANTITY);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [submitError, setSubmitError] = useState<ApiError | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const unavailable = product !== null && !product.inStock;

  async function submitOrder(): Promise<void> {
    if (submitting) return;
    const trimmedCustomerId = customerId.trim();
    const parsedQuantity = Number(quantity);

    const nextFieldErrors: FieldErrors = {};
    if (trimmedCustomerId === '') {
      nextFieldErrors.customerId = 'Enter a customer id.';
    }
    if (quantity.trim() === '' || !Number.isInteger(parsedQuantity) || parsedQuantity < 1) {
      nextFieldErrors.quantity = 'Enter a whole quantity of 1 or more.';
    }

    setSubmitError(null);
    setFieldErrors(nextFieldErrors);
    if (Object.keys(nextFieldErrors).length > 0) return;
    if (unavailable) return;

    setSubmitting(true);
    try {
      const order = await createOrder({
        customerId: trimmedCustomerId,
        lines: [{ productId, quantity: parsedQuantity }],
      });
      navigate(`/orders/${encodeURIComponent(order.orderId)}`);
    } catch (error) {
      const apiError = toApiError(error);
      if (apiError.code === 'validation_error') {
        setFieldErrors(extractFieldErrors(apiError));
      }
      setSubmitError(apiError);
    } finally {
      setSubmitting(false);
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    void submitOrder();
  }

  const stockMessage =
    submitError !== null && hasErrorCode(submitError, 'insufficient_stock')
      ? insufficientStockMessage(submitError, { productName: product?.name, productId })
      : undefined;

  return (
    <section className="screen" aria-labelledby="create-order-heading">
      <nav className="breadcrumb" aria-label="Breadcrumb">
        <Link to={`/products/${encodeURIComponent(productId)}`}>Back to product</Link>
      </nav>

      <h1 id="create-order-heading">Create order</h1>

      {productError !== null && <ErrorBanner error={productError} onRetry={reloadProduct} />}
      {productLoading && product === null && (
        <p className="state" role="status">
          Loading product…
        </p>
      )}

      {product !== null && (
        <p className="muted" data-testid="order-product-summary">
          {product.name} · {product.sku} · {formatMoney(product.price, product.currency)} ·{' '}
          {product.inStock ? 'Available' : 'Unavailable'}
        </p>
      )}

      {unavailable && (
        <p className="state" role="status" data-testid="create-order-unavailable">
          This product is out of stock, so a new order cannot be created for it.
        </p>
      )}

      <form className="form" onSubmit={handleSubmit} noValidate>
        <div className="field">
          <label htmlFor="customerId">Customer ID</label>
          <input
            id="customerId"
            name="customerId"
            type="text"
            value={customerId}
            onChange={(event) => setCustomerId(event.target.value)}
            autoComplete="off"
            aria-invalid={fieldErrors.customerId !== undefined}
            aria-describedby={fieldErrors.customerId !== undefined ? 'customerId-error' : undefined}
          />
          {fieldErrors.customerId !== undefined && (
            <p className="field__error" id="customerId-error" role="alert" data-testid="customerId-error">
              {fieldErrors.customerId}
            </p>
          )}
        </div>

        <div className="field">
          <label htmlFor="quantity">Quantity</label>
          <input
            id="quantity"
            name="quantity"
            type="number"
            min="1"
            step="1"
            inputMode="numeric"
            value={quantity}
            onChange={(event) => setQuantity(event.target.value)}
            aria-invalid={fieldErrors.quantity !== undefined}
            aria-describedby={fieldErrors.quantity !== undefined ? 'quantity-error' : undefined}
          />
          {fieldErrors.quantity !== undefined && (
            <p className="field__error" id="quantity-error" role="alert" data-testid="quantity-error">
              {fieldErrors.quantity}
            </p>
          )}
        </div>

        {submitError !== null && (
          <ErrorBanner
            error={submitError}
            message={stockMessage}
            onRetry={
              submitError.retryable
                ? () => {
                    void submitOrder();
                  }
                : undefined
            }
          />
        )}

        <button type="submit" className="button button--primary" disabled={submitting || unavailable}>
          {submitting ? 'Creating order…' : 'Create order'}
        </button>
      </form>
    </section>
  );
}

export default CreateOrder;
