import { fireEvent, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { OrderStatus } from './OrderStatus';
import { errorEnvelope, installGatewayMock, orderView, renderApp, renderRoute } from '../../test/testUtils';

const ORDER_ID = '6f1c1c1e-9f1a-4a8f-9f6b-2c9a4f0d1234';
const ORDER_URL = `http://localhost:8080/api/orders/${ORDER_ID}`;

function renderOrderStatus() {
  return renderRoute(<OrderStatus />, { path: '/orders/:orderId', route: `/orders/${ORDER_ID}` });
}

describe('OrderStatus', () => {
  it('shows status, timestamps, line items and total from the gateway', async () => {
    const mock = installGatewayMock(() => ({ body: orderView() }));

    renderOrderStatus();

    expect(await screen.findByTestId('order-status')).toHaveTextContent('CONFIRMED');
    expect(screen.getByTestId('order-status')).toHaveAttribute('data-status', 'CONFIRMED');
    expect(screen.getByTestId('order-created-at')).toHaveTextContent('2026-09-18T12:00:00Z');
    const line = screen.getByTestId('order-line-p-1001');
    expect(line).toHaveTextContent('ShopFlow Classic Tee');
    expect(screen.getByTestId('order-line-quantity-p-1001')).toHaveTextContent('1');
    expect(screen.getByTestId('order-line-unit-price-p-1001')).toHaveTextContent('$19.99');
    expect(screen.getByTestId('order-line-total-p-1001')).toHaveTextContent('$19.99');
    expect(screen.getByTestId('order-total')).toHaveTextContent('$19.99');
    expect(mock.requests[0]!.url).toBe(ORDER_URL);
  });

  it('shows shippedAt when the order has shipped', async () => {
    installGatewayMock(() => ({ body: orderView({ status: 'SHIPPED', shippedAt: '2026-09-18T13:00:00Z' }) }));

    renderOrderStatus();

    expect(await screen.findByTestId('order-status')).toHaveTextContent('SHIPPED');
    expect(screen.getByTestId('order-shipped-at')).toHaveTextContent('2026-09-18T13:00:00Z');
  });

  it('offers cancellation for a CONFIRMED order and hides it for a SHIPPED order', async () => {
    installGatewayMock(() => ({ body: orderView() }));
    const confirmedView = renderOrderStatus();
    await screen.findByTestId('open-cancel-dialog');
    confirmedView.unmount();

    installGatewayMock(() => ({ body: orderView({ status: 'SHIPPED', shippedAt: '2026-09-18T13:00:00Z' }) }));
    renderOrderStatus();

    await screen.findByTestId('order-status');
    expect(screen.queryByTestId('open-cancel-dialog')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Cancel order' })).not.toBeInTheDocument();
  });

  it('hides the cancel action for orders that are no longer cancellable', async () => {
    installGatewayMock(() => ({ body: orderView({ status: 'CANCELLED', cancelledAt: '2026-09-18T12:30:00Z', cancellationReason: 'changed mind' }) }));

    renderOrderStatus();

    await screen.findByTestId('order-status');
    expect(screen.queryByTestId('open-cancel-dialog')).not.toBeInTheDocument();
  });

  it('cancels through the dialog and renders the cancelled order from the gateway reply', async () => {
    const cancelled = orderView({
      status: 'CANCELLED',
      cancelledAt: '2026-09-18T12:30:00Z',
      cancellationReason: 'customer changed their mind',
    });
    const mock = installGatewayMock((request) => {
      if (request.method === 'POST') return { status: 200, body: cancelled };
      return { body: orderView() };
    });
    const user = userEvent.setup();

    renderOrderStatus();
    await user.click(await screen.findByTestId('open-cancel-dialog'));

    fireEvent.change(screen.getByLabelText('Cancellation reason'), { target: { value: 'customer changed their mind' } });
    await user.click(screen.getByTestId('confirm-cancellation'));

    expect(await screen.findByTestId('order-status')).toHaveTextContent('CANCELLED');
    expect(screen.getByTestId('order-cancelled-at')).toHaveTextContent('2026-09-18T12:30:00Z');
    expect(screen.getByTestId('order-cancellation-reason')).toHaveTextContent('customer changed their mind');
    expect(screen.queryByTestId('open-cancel-dialog')).not.toBeInTheDocument();
    expect(mock.requests.filter((request) => request.method === 'POST')).toHaveLength(1);
  });

  it('shows the shipped error and stops offering cancellation when the order shipped in the meantime', async () => {
    const shipped = orderView({ status: 'SHIPPED', shippedAt: '2026-09-18T13:00:00Z' });
    let cancelAttempted = false;
    installGatewayMock((request) => {
      if (request.method === 'POST') {
        cancelAttempted = true;
        return { status: 409, body: errorEnvelope('order_shipped', 'order has already been shipped', undefined, 'corr-shipped') };
      }
      return { body: cancelAttempted ? shipped : orderView() };
    });
    const user = userEvent.setup();

    renderOrderStatus();
    await user.click(await screen.findByTestId('open-cancel-dialog'));
    fireEvent.change(screen.getByLabelText('Cancellation reason'), { target: { value: 'customer changed their mind' } });
    await user.click(screen.getByTestId('confirm-cancellation'));

    const banner = await screen.findByTestId('error-banner');
    expect(banner).toHaveAttribute('data-error-code', 'order_shipped');
    expect(screen.getByTestId('error-message').textContent).toMatch(/shipped/i);
    expect(screen.getByTestId('error-correlation-id')).toHaveTextContent('corr-shipped');

    // Closing the dialog reloads the order from the gateway, which now reports SHIPPED.
    await user.click(screen.getByTestId('dismiss-cancel-dialog'));

    expect(await screen.findByTestId('order-status')).toHaveTextContent('SHIPPED');
    expect(screen.queryByTestId('open-cancel-dialog')).not.toBeInTheDocument();
  });

  it('refreshes on demand', async () => {
    const mock = installGatewayMock(() => ({ body: orderView() }));
    const user = userEvent.setup();

    renderOrderStatus();
    await screen.findByTestId('order-status');

    await user.click(screen.getByTestId('refresh-order'));

    expect(mock.requests.filter((request) => request.url === ORDER_URL)).toHaveLength(2);
  });

  it('shows a retryable error banner when the gateway is unreachable', async () => {
    let attempts = 0;
    installGatewayMock(() => {
      attempts += 1;
      return attempts === 1 ? { networkError: true } : { body: orderView() };
    });
    const user = userEvent.setup();

    renderApp({ route: `/orders/${ORDER_ID}` });

    const banner = await screen.findByTestId('error-banner');
    expect(banner).toHaveAttribute('data-error-code', 'network_unavailable');
    expect(banner).toHaveAttribute('data-retryable', 'true');
    expect(screen.getByTestId('error-correlation-id').textContent).not.toBe('');

    await user.click(screen.getByTestId('error-retry'));

    expect(await screen.findByTestId('order-status')).toHaveTextContent('CONFIRMED');
  });

  it('shows the not-found banner for an unknown order id', async () => {
    installGatewayMock(() => ({
      status: 404,
      body: errorEnvelope('not_found', 'order not found', undefined, 'corr-404'),
    }));

    renderOrderStatus();

    expect(await screen.findByTestId('error-banner')).toHaveAttribute('data-error-code', 'not_found');
    expect(screen.getByTestId('error-correlation-id')).toHaveTextContent('corr-404');
  });
});
