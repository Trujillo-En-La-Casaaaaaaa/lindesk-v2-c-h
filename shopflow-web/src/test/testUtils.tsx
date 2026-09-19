import { render } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { vi } from 'vitest';
import type { ReactElement } from 'react';
import { App } from '../App';
import type { OrderView, ProductView } from '../api/types';

/** Gateway route shapes used by the tests (contracts.md §10 seed data). */
export const SEEDED_PRODUCTS: ProductView[] = [
  {
    productId: 'p-1001',
    sku: 'SF-TEE-001',
    name: 'ShopFlow Classic Tee',
    description: 'Heavyweight cotton tee with the ShopFlow mark.',
    price: 19.99,
    currency: 'USD',
    availableQuantity: 50,
    inStock: true,
  },
  {
    productId: 'p-1002',
    sku: 'SF-MUG-002',
    name: 'ShopFlow Ceramic Mug',
    description: 'Ceramic mug for long builds.',
    price: 12.5,
    currency: 'USD',
    availableQuantity: 25,
    inStock: true,
  },
  {
    productId: 'p-1003',
    sku: 'SF-HOOD-003',
    name: 'ShopFlow Hoodie',
    description: 'Fleece hoodie with a gateway print.',
    price: 49,
    currency: 'USD',
    availableQuantity: 10,
    inStock: true,
  },
  {
    productId: 'p-1004',
    sku: 'SF-STICK-004',
    name: 'ShopFlow Sticker Pack',
    description: 'Six die-cut stickers.',
    price: 4.25,
    currency: 'USD',
    availableQuantity: 100,
    inStock: true,
  },
  {
    productId: 'p-1005',
    sku: 'SF-CAP-005',
    name: 'ShopFlow Cap',
    description: 'Structured cap, zero seeded stock.',
    price: 22,
    currency: 'USD',
    availableQuantity: 0,
    inStock: false,
  },
];

export function productView(overrides: Partial<ProductView> = {}): ProductView {
  const base = SEEDED_PRODUCTS[0]!;
  return {
    ...base,
    ...overrides,
    description: overrides.description ?? base.description,
  };
}

export function orderView(overrides: Partial<OrderView> = {}): OrderView {
  const base: OrderView = {
    orderId: '6f1c1c1e-9f1a-4a8f-9f6b-2c9a4f0d1234',
    status: 'CONFIRMED',
    customerId: 'customer-1',
    createdAt: '2026-09-18T12:00:00Z',
    updatedAt: '2026-09-18T12:00:01Z',
    confirmedAt: '2026-09-18T12:00:01Z',
    shippedAt: null,
    cancelledAt: null,
    cancellationReason: null,
    total: 19.99,
    lines: [
      {
        productId: 'p-1001',
        sku: 'SF-TEE-001',
        name: 'ShopFlow Classic Tee',
        quantity: 1,
        unitPrice: 19.99,
        lineTotal: 19.99,
      },
    ],
  };
  return { ...base, ...overrides };
}

export function errorEnvelope(
  code: string,
  message = `${code} from the gateway`,
  details?: unknown,
  correlationId = 'corr-1234',
): { error: { code: string; message: string; correlationId: string; details?: unknown } } {
  return {
    error: {
      code,
      message,
      correlationId,
      ...(details === undefined ? {} : { details }),
    },
  };
}

export interface RecordedRequest {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: any;
}

export interface MockReply {
  status?: number;
  body?: unknown;
  headers?: Record<string, string>;
  /** Simulates a transport failure (gateway not reachable). */
  networkError?: boolean;
}

export type MockHandler = (request: RecordedRequest) => MockReply | undefined;

export interface GatewayMock {
  requests: RecordedRequest[];
  handler: MockHandler;
}

function normaliseHeaders(headers: RequestInit['headers']): Record<string, string> {
  const result: Record<string, string> = {};
  if (!headers) return result;
  if (headers instanceof Array) {
    for (const [key, value] of headers) result[key.toLowerCase()] = String(value);
    return result;
  }
  if (typeof (headers as Headers).forEach === 'function') {
    (headers as Headers).forEach((value, key) => {
      result[key.toLowerCase()] = String(value);
    });
    return result;
  }
  for (const [key, value] of Object.entries(headers as Record<string, string>)) {
    result[key.toLowerCase()] = String(value);
  }
  return result;
}

function toResponse(reply: MockReply): Response {
  const status = reply.status ?? 200;
  const body = reply.body === undefined ? '' : JSON.stringify(reply.body);
  const headers = reply.headers ?? {};
  const response = {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get: (name: string) => {
        const wanted = name.toLowerCase();
        for (const [key, value] of Object.entries(headers)) {
          if (key.toLowerCase() === wanted) return value;
        }
        return null;
      },
    },
    text: async () => body,
  };
  return response as unknown as Response;
}

/**
 * Installs a deterministic fetch double. Every request is recorded so tests can
 * assert the exact gateway URL, method, headers and body the app produced.
 */
export function installGatewayMock(handler: MockHandler): GatewayMock {
  const requests: RecordedRequest[] = [];
  const fetchMock = vi.fn(async (input: unknown, init?: RequestInit) => {
    const request: RecordedRequest = {
      url: typeof input === 'string' ? input : String(input),
      method: (init?.method ?? 'GET').toUpperCase(),
      headers: normaliseHeaders(init?.headers),
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : null,
    };
    requests.push(request);

    const reply = handler(request);
    if (reply === undefined) {
      throw new Error(`Unexpected gateway request: ${request.method} ${request.url}`);
    }
    if (reply.networkError) {
      throw new TypeError('Failed to fetch');
    }
    return toResponse(reply);
  });

  vi.stubGlobal('fetch', fetchMock);
  return { requests, handler };
}

/** Renders the real application shell at a route. */
export function renderApp(options: { route?: string } = {}) {
  const route = options.route ?? '/';
  return render(
    <MemoryRouter initialEntries={[route]}>
      <App />
    </MemoryRouter>,
  );
}

/** Renders a single screen under a route pattern (for `useParams`/`useNavigate`). */
export function renderRoute(ui: ReactElement, options: { path?: string; route?: string } = {}) {
  const path = options.path ?? '*';
  const route = options.route ?? '/';
  return render(
    <MemoryRouter initialEntries={[route]}>
      <Routes>
        <Route path={path} element={ui} />
        <Route path="*" element={<p data-testid="unmatched-route">no route matched</p>} />
      </Routes>
    </MemoryRouter>,
  );
}
