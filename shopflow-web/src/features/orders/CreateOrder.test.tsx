import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { SEEDED_PRODUCTS, errorEnvelope, installGatewayMock, orderView, renderApp } from '../../test/testUtils';

const TEE = SEEDED_PRODUCTS[0]!;
const CAP = SEEDED_PRODUCTS[4]!;
const CREATED_ORDER = orderView({ status: 'CONFIRMED', total: 39.98, lines: [
  {
    productId: 'p-1001',
    sku: 'SF-TEE-001',
    name: 'ShopFlow Classic Tee',
    quantity: 2,
    unitPrice: 19.99,
    lineTotal: 39.98,
  },
] });

describe('CreateOrder', () => {
  it('refuses obviously invalid input before calling the gateway', async () => {
    const mock = installGatewayMock(() => ({ body: TEE }));
    const user = userEvent.setup();

    renderApp({ route: '/products/p-1001/order' });
    await screen.findByTestId('order-product-summary');

    await user.click(screen.getByRole('button', { name: 'Create order' }));

    expect(await screen.findByTestId('customerId-error')).toHaveTextContent(/customer id/i);
    expect(mock.requests).toHaveLength(1);
    expect(mock.requests[0]!.method).toBe('GET');

    await user.type(screen.getByLabelText('Customer ID'), 'customer-7');
    await user.clear(screen.getByLabelText('Quantity'));
    await user.type(screen.getByLabelText('Quantity'), '0');
    await user.click(screen.getByRole('button', { name: 'Create order' }));

    expect(await screen.findByTestId('quantity-error')).toHaveTextContent(/quantity of 1 or more/i);
    expect(mock.requests).toHaveLength(1);
  });

  it('posts the order and navigates to the order status screen on 201', async () => {
    const mock = installGatewayMock((request) => {
      if (request.method === 'POST' && request.url === 'http://localhost:8080/api/orders') {
        return { status: 201, body: CREATED_ORDER };
      }
      if (request.url === `http://localhost:8080/api/orders/${CREATED_ORDER.orderId}`) {
        return { body: CREATED_ORDER };
      }
      if (request.url === 'http://localhost:8080/api/products/p-1001') {
        return { body: TEE };
      }
      return undefined;
    });
    const user = userEvent.setup();

    renderApp({ route: '/products/p-1001/order' });
    await screen.findByTestId('order-product-summary');

    expect(screen.getByTestId('order-product-summary')).toHaveTextContent('ShopFlow Classic Tee');
    expect(screen.getByTestId('order-product-summary')).toHaveTextContent('$19.99');

    await user.type(screen.getByLabelText('Customer ID'), 'customer-7');
    await user.clear(screen.getByLabelText('Quantity'));
    await user.type(screen.getByLabelText('Quantity'), '2');
    await user.click(screen.getByRole('button', { name: 'Create order' }));

    const createRequest = mock.requests.find((request) => request.method === 'POST')!;
    expect(createRequest.url).toBe('http://localhost:8080/api/orders');
    expect(createRequest.body).toEqual({ customerId: 'customer-7', lines: [{ productId: 'p-1001', quantity: 2 }] });
    expect(createRequest.headers['x-correlation-id']).toBeTruthy();

    expect(await screen.findByTestId('order-status')).toHaveTextContent('CONFIRMED');
    expect(screen.getByTestId('order-line-p-1001')).toHaveTextContent('ShopFlow Classic Tee');
    expect(screen.getByTestId('order-total')).toHaveTextContent('$39.98');
    expect(mock.requests.some((request) => request.url.endsWith(`/api/orders/${CREATED_ORDER.orderId}`))).toBe(true);
  });

  it('explains a 409 insufficient_stock using the gateway details', async () => {
    installGatewayMock((request) => {
      if (request.method === 'POST') {
        return {
          status: 409,
          body: errorEnvelope(
            'insufficient_stock',
            'line p-1001 requested 5 but only 2 are available',
            { productId: 'p-1001', requested: 5, available: 2 },
            'corr-stock',
          ),
        };
      }
      return { body: TEE };
    });
    const user = userEvent.setup();

    renderApp({ route: '/products/p-1001/order' });
    await screen.findByTestId('order-product-summary');

    await user.type(screen.getByLabelText('Customer ID'), 'customer-7');
    await user.clear(screen.getByLabelText('Quantity'));
    await user.type(screen.getByLabelText('Quantity'), '5');
    await user.click(screen.getByRole('button', { name: 'Create order' }));

    const banner = await screen.findByTestId('error-banner');
    expect(banner).toHaveAttribute('data-error-code', 'insufficient_stock');
    expect(screen.getByTestId('error-message').textContent).toContain('ShopFlow Classic Tee');
    expect(screen.getByTestId('error-message').textContent).toContain('requested 5, only 2 available');
    expect(screen.getByTestId('error-correlation-id')).toHaveTextContent('corr-stock');
    // No navigation, no invented state: the form is still on screen.
    expect(screen.getByRole('button', { name: 'Create order' })).toBeInTheDocument();
  });

  it('shows field level feedback for a 400 validation_error', async () => {
    installGatewayMock((request) => {
      if (request.method === 'POST') {
        return {
          status: 400,
          body: errorEnvelope(
            'validation_error',
            'quantity must be a positive integer',
            { fields: { quantity: 'must be greater than 0' } },
            'corr-valid',
          ),
        };
      }
      return { body: TEE };
    });
    const user = userEvent.setup();

    renderApp({ route: '/products/p-1001/order' });
    await screen.findByTestId('order-product-summary');

    await user.type(screen.getByLabelText('Customer ID'), 'customer-7');
    await user.click(screen.getByRole('button', { name: 'Create order' }));

    expect(await screen.findByTestId('quantity-error')).toHaveTextContent('must be greater than 0');
    expect(screen.getByTestId('error-code')).toHaveTextContent('validation_error');
    expect(screen.getByTestId('error-correlation-id')).toHaveTextContent('corr-valid');
    expect(screen.getByLabelText('Quantity')).toHaveAttribute('aria-invalid', 'true');
  });

  it('blocks order creation for an out-of-stock product', async () => {
    const mock = installGatewayMock(() => ({ body: CAP }));

    renderApp({ route: '/products/p-1005/order' });

    expect(await screen.findByTestId('create-order-unavailable')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create order' })).toBeDisabled();
    expect(mock.requests).toHaveLength(1);
  });

  it('shows a retryable banner when the inventory service is unavailable', async () => {
    installGatewayMock((request) => {
      if (request.method === 'POST') {
        return { status: 503, body: errorEnvelope('inventory_unavailable', 'inventory unreachable', undefined, 'corr-503') };
      }
      return { body: TEE };
    });
    const user = userEvent.setup();

    renderApp({ route: '/products/p-1001/order' });
    await screen.findByTestId('order-product-summary');

    await user.type(screen.getByLabelText('Customer ID'), 'customer-7');
    await user.click(screen.getByRole('button', { name: 'Create order' }));

    const banner = await screen.findByTestId('error-banner');
    expect(banner).toHaveAttribute('data-error-code', 'inventory_unavailable');
    expect(banner).toHaveAttribute('data-retryable', 'true');
    expect(screen.getByTestId('error-retry')).toBeInTheDocument();
    expect(screen.getByTestId('error-correlation-id')).toHaveTextContent('corr-503');
  });
});
