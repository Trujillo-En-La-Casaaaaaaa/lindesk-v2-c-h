import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { getOrder } from '../../api/client';
import { ErrorBanner } from '../../components/ErrorBanner';
import { formatMoney, formatTimestamp } from '../../lib/format';
import { useAsyncResource } from '../../hooks/useAsyncResource';
import { CancelOrderDialog } from './CancelOrderDialog';
import type { OrderView } from '../../api/types';

/** Statuses the gateway still allows to be cancelled; SHIPPED orders are final. */
const CANCELLABLE_STATUSES: ReadonlySet<string> = new Set(['CREATED', 'CONFIRMED']);

/**
 * Order status screen: `GET /api/orders/{orderId}`. It renders the gateway
 * view as-is, refreshes on demand, and offers cancellation only where the
 * status allows it.
 */
export function OrderStatus() {
  const { orderId = '' } = useParams<{ orderId: string }>();
  const { data: order, error, loading, reload, replace } = useAsyncResource(() => getOrder(orderId), [orderId]);
  const [cancelDialogOpen, setCancelDialogOpen] = useState(false);

  const canCancel = order !== null && CANCELLABLE_STATUSES.has(order.status);

  function closeDialog(): void {
    setCancelDialogOpen(false);
    // Reflect gateway truth: a failed cancellation may mean the order moved on
    // (for example it was shipped meanwhile), so reload instead of guessing.
    if (canCancel) reload();
  }

  function handleCancelled(updated: OrderView): void {
    replace(updated);
    setCancelDialogOpen(false);
  }

  return (
    <section className="screen" aria-labelledby="order-heading">
      <nav className="breadcrumb" aria-label="Breadcrumb">
        <Link to="/">Back to catalog</Link>
      </nav>

      <div className="screen__header">
        <h1 id="order-heading">
          Order <span data-testid="order-id">{orderId}</span>
        </h1>
        <button type="button" className="button" onClick={reload} disabled={loading} data-testid="refresh-order">
          Refresh
        </button>
      </div>

      {error !== null && <ErrorBanner error={error} onRetry={reload} />}

      {loading && order === null && (
        <p className="state" role="status">
          Loading order…
        </p>
      )}

      {order !== null && (
        <>
          <dl className="facts">
            <div>
              <dt>Status</dt>
              <dd>
                <span className="status" data-testid="order-status" data-status={order.status}>
                  {order.status}
                </span>
              </dd>
            </div>
            <div>
              <dt>Customer</dt>
              <dd data-testid="order-customer">{order.customerId}</dd>
            </div>
            <div>
              <dt>Created</dt>
              <dd>
                <time dateTime={order.createdAt} data-testid="order-created-at">
                  {formatTimestamp(order.createdAt)}
                </time>
              </dd>
            </div>
            <div>
              <dt>Updated</dt>
              <dd>
                <time dateTime={order.updatedAt} data-testid="order-updated-at">
                  {formatTimestamp(order.updatedAt)}
                </time>
              </dd>
            </div>
            {order.shippedAt !== null && order.shippedAt !== '' && (
              <div>
                <dt>Ship time</dt>
                <dd>
                  <time dateTime={order.shippedAt} data-testid="order-shipped-at">
                    {formatTimestamp(order.shippedAt)}
                  </time>
                </dd>
              </div>
            )}
            {order.cancelledAt !== null && order.cancelledAt !== '' && (
              <div>
                <dt>Cancellation time</dt>
                <dd>
                  <time dateTime={order.cancelledAt} data-testid="order-cancelled-at">
                    {formatTimestamp(order.cancelledAt)}
                  </time>
                </dd>
              </div>
            )}
            {order.cancellationReason !== null && order.cancellationReason !== '' && (
              <div>
                <dt>Cancellation reason</dt>
                <dd data-testid="order-cancellation-reason">{order.cancellationReason}</dd>
              </div>
            )}
            <div>
              <dt>Total</dt>
              <dd data-testid="order-total">{formatMoney(order.total)}</dd>
            </div>
          </dl>

          <table className="table" data-testid="order-lines">
            <caption>Order lines</caption>
            <thead>
              <tr>
                <th scope="col">Product</th>
                <th scope="col">SKU</th>
                <th scope="col">Quantity</th>
                <th scope="col">Unit price</th>
                <th scope="col">Line total</th>
              </tr>
            </thead>
            <tbody>
              {order.lines.map((line) => (
                <tr key={`${line.productId}-${line.sku}`} data-testid={`order-line-${line.productId}`}>
                  <th scope="row">{line.name}</th>
                  <td>{line.sku}</td>
                  <td data-testid={`order-line-quantity-${line.productId}`}>{line.quantity}</td>
                  <td data-testid={`order-line-unit-price-${line.productId}`}>
                    {formatMoney(line.unitPrice)}
                  </td>
                  <td data-testid={`order-line-total-${line.productId}`}>{formatMoney(line.lineTotal)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {order.lines.length === 0 && (
            <p className="state" role="status" data-testid="order-lines-empty">
              This order has no lines.
            </p>
          )}

          <div className="actions">
            {canCancel ? (
              <button
                type="button"
                className="button"
                onClick={() => setCancelDialogOpen(true)}
                data-testid="open-cancel-dialog"
              >
                Cancel order
              </button>
            ) : (
              <p className="muted" data-testid="cancellation-not-offered">
                This order is final - no further action is available.
              </p>
            )}
          </div>

          {cancelDialogOpen && (
            <CancelOrderDialog orderId={order.orderId} onClose={closeDialog} onCancelled={handleCancelled} />
          )}
        </>
      )}
    </section>
  );
}

export default OrderStatus;
