import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import {
  ADMIN_ROLE_HEADER,
  DEFAULT_GATEWAY_BASE_URL,
  cancelOrder,
  createOrder,
  getGatewayBaseUrl,
  getProduct,
  getOrder,
  listOrders,
  listProducts,
  shipOrder,
} from './client';
import { ApiError, isApiError } from './errors';
import { errorEnvelope, installGatewayMock, orderView, productView } from '../test/testUtils';

/** Vitest runs from the repository root; the guards below inspect real files. */
const SOURCE_DIR = join(process.cwd(), 'src');

function listSourceFiles(dir: string): string[] {
  const files: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      files.push(...listSourceFiles(full));
    } else if (/\.(ts|tsx|css|html)$/.test(entry)) {
      files.push(full);
    }
  }
  return files;
}

function isTestFile(path: string): boolean {
  const normalised = path.split('\\').join('/');
  return normalised.includes('.test.') || normalised.includes('/test/');
}

describe('gateway client configuration', () => {
  it('uses the configured gateway base URL and trims trailing slashes', () => {
    expect(getGatewayBaseUrl()).toBe(DEFAULT_GATEWAY_BASE_URL);

    vi.stubEnv('VITE_GATEWAY_BASE_URL', 'http://gateway.test:9999/');
    expect(getGatewayBaseUrl()).toBe('http://gateway.test:9999');
    expect(DEFAULT_GATEWAY_BASE_URL).toBe('http://localhost:8080');
  });

  it('calls the gateway base URL from the environment', async () => {
    vi.stubEnv('VITE_GATEWAY_BASE_URL', 'http://gateway.test:9999');
    const mock = installGatewayMock(() => ({ body: { items: [], total: 0 } }));

    await listProducts();

    expect(mock.requests).toHaveLength(1);
    expect(mock.requests[0]!.url).toBe('http://gateway.test:9999/api/products');
  });
});

describe('gateway client requests', () => {
  it('lists products from GET /api/products with a correlation id', async () => {
    const mock = installGatewayMock((request) => {
      if (request.method === 'GET' && request.url === 'http://localhost:8080/api/products') {
        return { body: { items: [productView()], total: 1 } };
      }
      return undefined;
    });

    const result = await listProducts();

    expect(result.total).toBe(1);
    expect(result.items[0]!.productId).toBe('p-1001');
    expect(mock.requests[0]!.headers['x-correlation-id']).toMatch(/[0-9a-zA-Z-]{8,}/);
    expect(mock.requests[0]!.headers['accept']).toBe('application/json');
  });

  it('passes paging parameters and tolerates a bare array response', async () => {
    const mock = installGatewayMock(() => ({ body: [productView()] }));

    const result = await listProducts({ limit: 5, offset: 10 });

    expect(mock.requests[0]!.url).toBe('http://localhost:8080/api/products?limit=5&offset=10');
    expect(result).toEqual({ items: [productView()], total: 1 });
  });

  it('gets a product and maps a 404 to the not_found code', async () => {
    const mock = installGatewayMock(() => ({
      status: 404,
      body: errorEnvelope('not_found', 'product p-9999 does not exist', undefined, 'corr-404'),
    }));

    const error = await getProduct('p-9999').catch((thrown: unknown) => thrown);

    expect(mock.requests[0]!.url).toBe('http://localhost:8080/api/products/p-9999');
    expect(isApiError(error)).toBe(true);
    const apiError = error as ApiError;
    expect(apiError.code).toBe('not_found');
    expect(apiError.correlationId).toBe('corr-404');
    expect(apiError.httpStatus).toBe(404);
    expect(apiError.retryable).toBe(false);
  });

  it('creates an order with POST /api/orders', async () => {
    const mock = installGatewayMock(() => ({ status: 201, body: orderView() }));

    const order = await createOrder({ customerId: 'customer-7', lines: [{ productId: 'p-1001', quantity: 2 }] });

    expect(order.status).toBe('CONFIRMED');
    expect(mock.requests[0]!.method).toBe('POST');
    expect(mock.requests[0]!.url).toBe('http://localhost:8080/api/orders');
    expect(mock.requests[0]!.headers['content-type']).toContain('application/json');
    expect(mock.requests[0]!.body).toEqual({ customerId: 'customer-7', lines: [{ productId: 'p-1001', quantity: 2 }] });
  });

  it('keeps the insufficient_stock details for the caller', async () => {
    installGatewayMock(() => ({
      status: 409,
      body: errorEnvelope(
        'insufficient_stock',
        'line p-1001 requested 99 but only 50 are available',
        { productId: 'p-1001', requested: 99, available: 50 },
        'corr-stock',
      ),
    }));

    const error = (await createOrder({
      customerId: 'customer-7',
      lines: [{ productId: 'p-1001', quantity: 99 }],
    }).catch((thrown: unknown) => thrown)) as ApiError;

    expect(error.code).toBe('insufficient_stock');
    expect(error.details).toEqual({ productId: 'p-1001', requested: 99, available: 50 });
    expect(error.retryable).toBe(false);
  });

  it('treats a 503 inventory_unavailable as a retryable failure', async () => {
    installGatewayMock(() => ({
      status: 503,
      body: errorEnvelope('inventory_unavailable', 'inventory unreachable', undefined, 'corr-503'),
    }));

    const error = (await createOrder({
      customerId: 'customer-7',
      lines: [{ productId: 'p-1001', quantity: 1 }],
    }).catch((thrown: unknown) => thrown)) as ApiError;

    expect(error.code).toBe('inventory_unavailable');
    expect(error.retryable).toBe(true);
    expect(error.correlationId).toBe('corr-503');
  });

  it('reads an order and filters the order list by customer', async () => {
    const mock = installGatewayMock((request) => {
      if (request.url.startsWith('http://localhost:8080/api/orders/')) return { body: orderView() };
      return { body: { items: [orderView()], total: 1 } };
    });

    const order = await getOrder('abc123');
    const list = await listOrders({ customerId: 'customer-7' });

    expect(order.orderId).toBe(orderView().orderId);
    expect(list.items).toHaveLength(1);
    expect(mock.requests[0]!.url).toBe('http://localhost:8080/api/orders/abc123');
    expect(mock.requests[1]!.url).toBe('http://localhost:8080/api/orders?customerId=customer-7');
  });

  it('cancels an order with a reason and mirrors the gateway error codes', async () => {
    const mock = installGatewayMock(() => ({
      status: 200,
      body: orderView({
        status: 'CANCELLED',
        cancelledAt: '2026-09-18T12:05:00Z',
        cancellationReason: 'customer changed their mind',
      }),
    }));

    const cancelled = await cancelOrder('order-1', { reason: 'customer changed their mind' });

    expect(cancelled.status).toBe('CANCELLED');
    expect(cancelled.cancelledAt).toBe('2026-09-18T12:05:00Z');
    expect(mock.requests[0]!.url).toBe('http://localhost:8080/api/orders/order-1/cancel');
    expect(mock.requests[0]!.body).toEqual({ reason: 'customer changed their mind' });

    installGatewayMock(() => ({ status: 400, body: errorEnvelope('invalid_reason', 'reason too long') }));
    const invalidReason = (await cancelOrder('order-1', { reason: 'x' }).catch((thrown: unknown) => thrown)) as ApiError;
    expect(invalidReason.code).toBe('invalid_reason');

    installGatewayMock(() => ({ status: 409, body: errorEnvelope('order_shipped', 'order already shipped') }));
    const shipped = (await cancelOrder('order-1', { reason: 'x' }).catch((thrown: unknown) => thrown)) as ApiError;
    expect(shipped.code).toBe('order_shipped');
    expect(shipped.retryable).toBe(false);

    installGatewayMock(() => ({ status: 409, body: errorEnvelope('order_not_cancellable', 'order failed') }));
    const notCancellable = (await cancelOrder('order-1', { reason: 'x' }).catch((thrown: unknown) => thrown)) as ApiError;
    expect(notCancellable.code).toBe('order_not_cancellable');
  });

  it('ships an order with the admin role header', async () => {
    const mock = installGatewayMock(() => ({
      status: 200,
      body: orderView({ status: 'SHIPPED', shippedAt: '2026-09-18T13:00:00Z' }),
    }));

    const shipped = await shipOrder('order-1');

    expect(shipped.status).toBe('SHIPPED');
    expect(mock.requests[0]!.method).toBe('POST');
    expect(mock.requests[0]!.url).toBe('http://localhost:8080/api/orders/order-1/ship');
    expect(mock.requests[0]!.headers[ADMIN_ROLE_HEADER.toLowerCase()]).toBe('admin');
    expect(mock.requests[0]!.body).toBeNull();
  });

  it('surfaces a 409 invalid_status_transition from the ship route', async () => {
    installGatewayMock(() => ({
      status: 409,
      body: errorEnvelope('invalid_status_transition', 'order is CREATED', undefined, 'corr-ship'),
    }));

    const error = (await shipOrder('order-1').catch((thrown: unknown) => thrown)) as ApiError;

    expect(error.code).toBe('invalid_status_transition');
    expect(error.correlationId).toBe('corr-ship');
  });
});

describe('gateway client failure semantics', () => {
  it('reports an unreachable gateway as a retryable failure with a correlation id', async () => {
    const mock = installGatewayMock(() => ({ networkError: true }));

    const error = (await listProducts().catch((thrown: unknown) => thrown)) as ApiError;

    expect(error.code).toBe('network_unavailable');
    expect(error.retryable).toBe(true);
    expect(error.httpStatus).toBeNull();
    expect(error.correlationId).toBe(mock.requests[0]!.headers['x-correlation-id']);
    expect(error.correlationId).not.toBe('');
  });

  it('falls back to the status code when a failure carries no envelope', async () => {
    installGatewayMock(() => ({ status: 502, body: undefined }));

    const error = (await listProducts().catch((thrown: unknown) => thrown)) as ApiError;

    expect(error.code).toBe('upstream_unavailable');
    expect(error.retryable).toBe(true);
  });

  it('reads the correlation id from the response header when the envelope omits it', async () => {
    installGatewayMock(() => ({
      status: 500,
      body: undefined,
      headers: { 'X-Correlation-Id': 'corr-from-header' },
    }));

    const error = (await listProducts().catch((thrown: unknown) => thrown)) as ApiError;

    expect(error.correlationId).toBe('corr-from-header');
    expect(error.retryable).toBe(true);
  });
});

describe('gateway boundary', () => {
  it('is the only application module that performs HTTP', () => {
    // Test files only install a fetch double, so they are not application modules.
    const offenders = listSourceFiles(SOURCE_DIR)
      .filter((file) => !isTestFile(file))
      .filter((file) => !file.replace(/\\/g, '/').endsWith('src/api/client.ts'))
      .filter((file) => {
        const content = readFileSync(file, 'utf8');
        return /\bfetch\s*\(|XMLHttpRequest|sendBeacon|\baxios\b|new WebSocket|EventSource/.test(content);
      })
      .map((file) => file.replace(/\\/g, '/'));

    expect(offenders).toEqual([]);
    expect(readFileSync(join(SOURCE_DIR, 'api', 'client.ts'), 'utf8')).toMatch(/fetch\s*\(/);
  });

  it('configures no other base URL than the gateway', () => {
    const serviceMarkers = [['shopflow', 'orders'].join('-'), ['shopflow', 'inventory'].join('-'), ['shopflow', 'notifications'].join('-'), 'PROVIDER_BASE_URL', 'INVENTORY_BASE_URL', 'ORDERS_BASE_URL'];
    const urls: string[] = [];

    for (const file of listSourceFiles(SOURCE_DIR)) {
      if (isTestFile(file)) continue;
      const content = readFileSync(file, 'utf8');
      for (const marker of serviceMarkers) {
        expect(content, `${file} must not reference ${marker}`).not.toContain(marker);
      }
      for (const match of content.matchAll(/https?:\/\/[^\s"'`)]+/g)) {
        urls.push(match[0]);
      }
    }

    expect(urls.length).toBeGreaterThan(0);
    for (const url of urls) {
      expect(url.startsWith(DEFAULT_GATEWAY_BASE_URL)).toBe(true);
    }
  });

  it('reads the gateway base URL from the single documented environment variable', () => {
    const client = readFileSync(join(SOURCE_DIR, 'api', 'client.ts'), 'utf8');
    const envReferences = client.match(/import\.meta\.env\.[A-Z_]+/g) ?? [];

    expect(envReferences).toEqual(['import.meta.env.VITE_GATEWAY_BASE_URL']);
  });
});
