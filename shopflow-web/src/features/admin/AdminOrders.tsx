import { useState } from 'react';
import { Link } from 'react-router-dom';
import { listOrders, shipOrder } from '../../api/client';
import { ErrorBanner } from '../../components/ErrorBanner';
import { toApiError } from '../../api/errors';
import type { ApiError } from '../../api/errors';
import type { OrderView } from '../../api/types';
import { useAsyncResource } from '../../hooks/useAsyncResource';
import { formatMoney, formatTimestamp } from '../../lib/format';

/**
 * Administrative screen: lists orders (`GET /api/orders`) and marks a CONFIRMED
 * order as SHIPPED (`POST /api/orders/{orderId}/ship` with the admin role
 * header). The action is only offered where the status allows it, and a
 * `409 invalid_status_transition` is surfaced as it happens.
 */
export function AdminOrders() {
  const { data, error, loading, reload, replace } = useAsyncResource(() => listOrders(), []);
  const orders = data?.items ?? [];

  const [shipErrors, setShipErrors] = useState<Record<string, ApiError>>({});
  const [shippingOrderId, setShippingOrderId] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState('');

  async function ship(order: OrderView): Promise<void> {
    if (shippingOrderId !== null) return;
    setShippingOrderId(order.orderId);
    setAnnouncement('');
    setShipErrors((previous) => {
      const next = { ...previous };
      delete next[order.orderId];
      return next;
    });

    try {
      const updated = await shipOrder(order.orderId);
      replace({
        items: orders.map((item) => (item.orderId === updated.orderId ? updated : item)),
        total: data?.total ?? orders.length,
      });
      setAnnouncement(`Order ${order.orderId} updated.`);
    } catch (error) {
      setShipErrors((previous) => ({ ...previous, [order.orderId]: toApiError(error) }));
    } finally {
      setShippingOrderId(null);
    }
  }

  return (
    <section className="screen" aria-labelledby="admin-heading">
      <div className="screen__header">
        <h1 id="admin-heading">Admin orders</h1>
        <button type="button" className="button" onClick={reload} disabled={loading} data-testid="refresh-orders">
          Refresh
        </button>
      </div>

      <p className="muted">Marking an order as shipped is an administrative action and requires the admin role.</p>

      <p className="visually-hidden" role="status" data-testid="admin-announcement">
        {announcement}
      </p>

      {error !== null && <ErrorBanner error={error} onRetry={reload} />}

      {loading && orders.length === 0 && (
        <p className="state" role="status">
          Loading orders…
        </p>
      )}

      {!loading && error === null && orders.length === 0 && (
        <p className="state" role="status" data-testid="orders-empty">
          No orders have been created yet.
        </p>
      )}

      {orders.length > 0 && (
        <>
          <table className="table" data-testid="order-table">
            <caption>Orders ({orders.length})</caption>
            <thead>
              <tr>
                <th scope="col">Order</th>
                <th scope="col">Customer</th>
                <th scope="col">Status</th>
                <th scope="col">Created</th>
                <th scope="col">Total</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((order) => (
                <tr key={order.orderId} data-testid={`order-row-${order.orderId}`}>
                  <th scope="row">
                    <Link to={`/orders/${encodeURIComponent(order.orderId)}`}>{order.orderId}</Link>
                  </th>
                  <td>{order.customerId}</td>
                  <td data-testid={`order-status-${order.orderId}`}>{order.status}</td>
                  <td>
                    <time dateTime={order.createdAt}>{formatTimestamp(order.createdAt)}</time>
                  </td>
                  <td>{formatMoney(order.total)}</td>
                  <td>
                    {order.status === 'CONFIRMED' ? (
                      <button
                        type="button"
                        className="button"
                        onClick={() => {
                          void ship(order);
                        }}
                        disabled={shippingOrderId !== null}
                        title={`Mark order ${order.orderId} as shipped`}
                        data-testid={`ship-order-${order.orderId}`}
                      >
                        Ship
                      </button>
                    ) : (
                      <span className="muted" data-testid={`no-action-${order.orderId}`}>
                        No action available
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <ul className="error-list">
            {Object.entries(shipErrors).map(([orderId, shipError]) => (
              <li key={orderId}>
                <p className="muted">Order {orderId}</p>
                <ErrorBanner error={shipError} />
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

export default AdminOrders;
