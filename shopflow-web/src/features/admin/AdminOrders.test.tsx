import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { AdminOrders } from './AdminOrders';
import { errorEnvelope, installGatewayMock, orderView, renderRoute } from '../../test/testUtils';

const CONFIRMED = orderView({ orderId: 'order-confirmed', status: 'CONFIRMED' });
const SHIPPED = orderView({ orderId: 'order-shipped', status: 'SHIPPED', shippedAt: '2026-09-18T13:00:00Z' });
const CANCELLED = orderView({ orderId: 'order-cancelled', status: 'CANCELLED', cancelledAt: '2026-09-18T12:30:00Z' });

function renderAdmin() {
  return renderRoute(<AdminOrders />, { path: '/admin/orders', route: '/admin/orders' });
}

describe('AdminOrders', () => {
  it('lists orders and only offers the ship action for CONFIRMED orders', async () => {
    const mock = installGatewayMock(() => ({ body: { items: [CONFIRMED, SHIPPED, CANCELLED], total: 3 } }));

    renderAdmin();

    expect(await screen.findByTestId('order-table')).toBeInTheDocument();
    expect(mock.requests[0]!.url).toBe('http://localhost:8080/api/orders');
    expect(within(screen.getByTestId('order-row-order-confirmed')).getByTestId('ship-order-order-confirmed')).toBeEnabled();
    expect(within(screen.getByTestId('order-row-order-shipped')).queryByRole('button', { name: 'Ship' })).not.toBeInTheDocument();
    expect(within(screen.getByTestId('order-row-order-cancelled')).queryByRole('button', { name: 'Ship' })).not.toBeInTheDocument();
    expect(screen.getByTestId('order-status-order-shipped')).toHaveTextContent('SHIPPED');
  });

  it('ships a CONFIRMED order with the admin role header and reflects the gateway reply', async () => {
    const shippedReply = orderView({ ...CONFIRMED, status: 'SHIPPED', shippedAt: '2026-09-18T13:05:00Z' });
    const mock = installGatewayMock((request) => {
      if (request.method === 'POST') return { status: 200, body: shippedReply };
      return { body: { items: [CONFIRMED, SHIPPED], total: 2 } };
    });
    const user = userEvent.setup();

    renderAdmin();
    await user.click(await screen.findByTestId('ship-order-order-confirmed'));

    const shipRequest = mock.requests.find((request) => request.method === 'POST')!;
    expect(shipRequest.url).toBe('http://localhost:8080/api/orders/order-confirmed/ship');
    expect(shipRequest.headers['x-shopflow-role']).toBe('admin');
    expect(shipRequest.headers['x-correlation-id']).toBeTruthy();

    expect(await screen.findByTestId('order-status-order-confirmed')).toHaveTextContent('SHIPPED');
    expect(screen.queryByTestId('ship-order-order-confirmed')).not.toBeInTheDocument();
    // The other order was not touched.
    expect(screen.getByTestId('order-status-order-shipped')).toHaveTextContent('SHIPPED');
  });

  it('surfaces a 409 invalid_status_transition with its correlation id', async () => {
    installGatewayMock((request) => {
      if (request.method === 'POST') {
        return {
          status: 409,
          body: errorEnvelope('invalid_status_transition', 'order is not CONFIRMED', undefined, 'corr-status'),
        };
      }
      return { body: { items: [CONFIRMED], total: 1 } };
    });
    const user = userEvent.setup();

    renderAdmin();
    await user.click(await screen.findByTestId('ship-order-order-confirmed'));

    const banner = await screen.findByTestId('error-banner');
    expect(banner).toHaveAttribute('data-error-code', 'invalid_status_transition');
    expect(screen.getByTestId('error-message').textContent).toMatch(/confirmed order/i);
    expect(screen.getByTestId('error-correlation-id')).toHaveTextContent('corr-status');
    // The row keeps the status the gateway last reported.
    expect(screen.getByTestId('order-status-order-confirmed')).toHaveTextContent('CONFIRMED');
  });

  it('shows the empty state when there are no orders', async () => {
    installGatewayMock(() => ({ body: { items: [], total: 0 } }));

    renderAdmin();

    expect(await screen.findByTestId('orders-empty')).toHaveTextContent(/no orders/i);
  });

  it('shows a retryable banner when the gateway is unreachable', async () => {
    installGatewayMock(() => ({ networkError: true }));

    renderAdmin();

    const banner = await screen.findByTestId('error-banner');
    expect(banner).toHaveAttribute('data-error-code', 'network_unavailable');
    expect(banner).toHaveAttribute('data-retryable', 'true');
    expect(screen.queryByTestId('orders-empty')).not.toBeInTheDocument();
  });
});
