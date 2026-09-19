import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { ProductList } from './ProductList';
import { SEEDED_PRODUCTS, errorEnvelope, installGatewayMock, renderRoute } from '../../test/testUtils';

function renderCatalog() {
  return renderRoute(<ProductList />, { path: '/', route: '/' });
}

describe('ProductList', () => {
  it('shows the loading state while the catalog request is in flight', () => {
    installGatewayMock(() => ({ body: { items: SEEDED_PRODUCTS, total: SEEDED_PRODUCTS.length } }));

    renderCatalog();

    expect(screen.getByRole('status')).toHaveTextContent(/loading products/i);
    expect(screen.queryByTestId('product-table')).not.toBeInTheDocument();

    return screen.findByText('ShopFlow Classic Tee');
  });

  it('lists the five seeded products with price and availability', async () => {
    const mock = installGatewayMock(() => ({ body: { items: SEEDED_PRODUCTS, total: SEEDED_PRODUCTS.length } }));

    renderCatalog();

    expect(await screen.findByText('ShopFlow Classic Tee')).toBeInTheDocument();
    for (const product of SEEDED_PRODUCTS) {
      expect(screen.getByText(product.name)).toBeInTheDocument();
      expect(screen.getByTestId(`product-row-${product.productId}`)).toBeInTheDocument();
    }

    expect(mock.requests).toHaveLength(1);
    expect(mock.requests[0]!.method).toBe('GET');
    expect(mock.requests[0]!.url).toBe('http://localhost:8080/api/products');
    expect(mock.requests[0]!.headers['x-correlation-id']).toBeTruthy();

    expect(screen.getByTestId('product-price-p-1001')).toHaveTextContent('$19.99');
    expect(screen.getByTestId('product-availability-p-1001')).toHaveTextContent('Available (50 in stock)');
    expect(screen.getByTestId('catalog-count')).toHaveTextContent('Showing 5 of 5 products.');
  });

  it('shows p-1005 as unavailable with its order action disabled', async () => {
    installGatewayMock(() => ({ body: { items: SEEDED_PRODUCTS, total: SEEDED_PRODUCTS.length } }));

    renderCatalog();
    await screen.findByText('ShopFlow Cap');

    const zeroStockRow = screen.getByTestId('product-row-p-1005');
    expect(within(zeroStockRow).getByText('Unavailable')).toBeInTheDocument();
    expect(within(zeroStockRow).getByRole('button', { name: 'Order' })).toBeDisabled();
    expect(within(zeroStockRow).queryByRole('link', { name: 'Order' })).not.toBeInTheDocument();

    const inStockRow = screen.getByTestId('product-row-p-1001');
    expect(within(inStockRow).getByRole('link', { name: 'Order' })).toHaveAttribute(
      'href',
      '/products/p-1001/order',
    );
  });

  it('shows the empty state when the catalog has no products', async () => {
    installGatewayMock(() => ({ body: { items: [], total: 0 } }));

    renderCatalog();

    expect(await screen.findByTestId('catalog-empty')).toHaveTextContent(/no products/i);
  });

  it('shows a retryable error banner with a correlation id when the gateway is down', async () => {
    let attempts = 0;
    const mock = installGatewayMock(() => {
      attempts += 1;
      if (attempts === 1) return { networkError: true };
      return { body: { items: SEEDED_PRODUCTS, total: SEEDED_PRODUCTS.length } };
    });
    const user = userEvent.setup();

    renderCatalog();

    const banner = await screen.findByTestId('error-banner');
    expect(banner).toHaveAttribute('data-error-code', 'network_unavailable');
    expect(banner).toHaveAttribute('data-retryable', 'true');
    expect(screen.getByTestId('error-correlation-id').textContent).not.toBe('');
    expect(screen.getByTestId('error-correlation-id')).toHaveTextContent(
      mock.requests[0]!.headers['x-correlation-id']!,
    );

    await user.click(screen.getByTestId('error-retry'));

    expect(await screen.findByText('ShopFlow Classic Tee')).toBeInTheDocument();
    expect(mock.requests).toHaveLength(2);
  });

  it('reports a gateway error envelope without inventing state', async () => {
    installGatewayMock(() => ({
      status: 503,
      body: errorEnvelope('upstream_unavailable', 'gateway could not reach a service', undefined, 'corr-503'),
    }));

    renderCatalog();

    const banner = await screen.findByTestId('error-banner');
    expect(banner).toHaveAttribute('data-error-code', 'upstream_unavailable');
    expect(screen.getByTestId('error-correlation-id')).toHaveTextContent('corr-503');
    expect(screen.queryByTestId('product-table')).not.toBeInTheDocument();
    expect(screen.queryByTestId('catalog-empty')).not.toBeInTheDocument();
  });
});
