import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { SEEDED_PRODUCTS, installGatewayMock, orderView, renderApp } from './test/testUtils';

const INDEX_HTML = join(process.cwd(), 'index.html');

describe('App shell', () => {
  it('renders the catalog at / and links to the admin screen', async () => {
    installGatewayMock(() => ({ body: { items: SEEDED_PRODUCTS, total: SEEDED_PRODUCTS.length } }));

    renderApp({ route: '/' });

    expect(await screen.findByRole('heading', { name: 'Catalog' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Catalog' })).toHaveAttribute('href', '/');
    expect(screen.getByRole('link', { name: 'Admin orders' })).toHaveAttribute('href', '/admin/orders');
    expect(screen.getByRole('main')).toBeInTheDocument();
  });

  it('navigates from the catalog to a product and to the admin screen', async () => {
    installGatewayMock((request) => {
      if (request.url === 'http://localhost:8080/api/products') {
        return { body: { items: SEEDED_PRODUCTS, total: SEEDED_PRODUCTS.length } };
      }
      if (request.url === 'http://localhost:8080/api/products/p-1001') {
        return { body: SEEDED_PRODUCTS[0]! };
      }
      if (request.url === 'http://localhost:8080/api/orders') {
        return { body: { items: [orderView()], total: 1 } };
      }
      return undefined;
    });
    const user = userEvent.setup();

    renderApp({ route: '/' });
    await screen.findByText('ShopFlow Classic Tee');

    await user.click(screen.getByRole('link', { name: 'ShopFlow Classic Tee' }));
    expect(await screen.findByTestId('product-name')).toHaveTextContent('ShopFlow Classic Tee');

    await user.click(screen.getByRole('link', { name: 'Admin orders' }));
    expect(await screen.findByRole('heading', { name: 'Admin orders' })).toBeInTheDocument();
  });

  it('shows a not-found screen for an unknown route', async () => {
    installGatewayMock(() => ({ body: { items: [], total: 0 } }));

    renderApp({ route: '/does-not-exist' });

    expect(await screen.findByTestId('not-found')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Back to the catalog' })).toHaveAttribute('href', '/');
  });

  it('ships no external assets or CDN references', () => {
    const html = readFileSync(INDEX_HTML, 'utf8');

    expect(html).not.toMatch(/https?:\/\//);
    expect(html).not.toMatch(/<script[^>]+src="(?!\/src\/main\.tsx)/);
    expect(html).toContain('<div id="root"></div>');
  });
});
