import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { ProductDetail } from './ProductDetail';
import { SEEDED_PRODUCTS, errorEnvelope, installGatewayMock, renderRoute } from '../../test/testUtils';

const TEE = SEEDED_PRODUCTS[0]!;
const CAP = SEEDED_PRODUCTS[4]!;

function renderDetail(productId: string) {
  return renderRoute(<ProductDetail />, {
    path: '/products/:productId',
    route: `/products/${productId}`,
  });
}

describe('ProductDetail', () => {
  it('shows name, description, price and available quantity', async () => {
    const mock = installGatewayMock(() => ({ body: TEE }));

    renderDetail('p-1001');

    expect(await screen.findByTestId('product-name')).toHaveTextContent('ShopFlow Classic Tee');
    expect(screen.getByTestId('product-description')).toHaveTextContent(TEE.description);
    expect(screen.getByTestId('product-price')).toHaveTextContent('$19.99');
    expect(screen.getByTestId('product-available-quantity')).toHaveTextContent('50');
    expect(screen.getByTestId('product-availability')).toHaveTextContent('Available');
    expect(screen.getByTestId('create-order-link')).toHaveAttribute('href', '/products/p-1001/order');
    expect(mock.requests[0]!.url).toBe('http://localhost:8080/api/products/p-1001');
  });

  it('shows a zero-stock product as unavailable with the order action disabled', async () => {
    installGatewayMock(() => ({ body: CAP }));

    renderDetail('p-1005');

    expect(await screen.findByTestId('product-name')).toHaveTextContent('ShopFlow Cap');
    expect(screen.getByTestId('product-available-quantity')).toHaveTextContent('0');
    expect(screen.getByTestId('product-availability')).toHaveTextContent('Unavailable');
    expect(screen.getByTestId('create-order-link')).toBeDisabled();
    expect(screen.getByTestId('unavailable-notice')).toBeInTheDocument();
  });

  it('shows a not-found state and the error banner for an unknown product', async () => {
    installGatewayMock(() => ({
      status: 404,
      body: errorEnvelope('not_found', 'product p-9999 does not exist', undefined, 'corr-404'),
    }));

    renderDetail('p-9999');

    expect(await screen.findByTestId('product-not-found')).toHaveTextContent(/could not be found/i);
    expect(screen.getByTestId('error-banner')).toHaveAttribute('data-error-code', 'not_found');
    expect(screen.getByTestId('error-correlation-id')).toHaveTextContent('corr-404');
  });

  it('offers a retry when the gateway is unreachable', async () => {
    let attempts = 0;
    const mock = installGatewayMock(() => {
      attempts += 1;
      return attempts === 1 ? { networkError: true } : { body: TEE };
    });
    const user = userEvent.setup();

    renderDetail('p-1001');

    const banner = await screen.findByTestId('error-banner');
    expect(banner).toHaveAttribute('data-retryable', 'true');

    await user.click(screen.getByTestId('error-retry'));

    expect(await screen.findByTestId('product-name')).toHaveTextContent('ShopFlow Classic Tee');
    expect(mock.requests).toHaveLength(2);
  });
});
