/**
 * The single HTTP client for ShopFlow Web.
 *
 * Every network call in this application goes through this module; no other
 * module performs HTTP. It reads the one configured base URL
 * (`VITE_GATEWAY_BASE_URL`), attaches `X-Correlation-Id` to every request, adds
 * the admin role header where the contract requires it, and maps failures to
 * {@link ApiError} built from the gateway error envelope (contracts.md §1, §2).
 */

import {
  ApiError,
  NETWORK_ERROR_CODE,
  UNKNOWN_ERROR_CODE,
  createCorrelationId,
  isRetryableCode,
} from './errors';
import type {
  CancelOrderRequest,
  CreateOrderRequest,
  ErrorEnvelope,
  ListOrdersParams,
  ListProductsParams,
  ListResponse,
  OrderListResponse,
  OrderView,
  ProductListResponse,
  ProductView,
} from './types';

/** Local default of the gateway base URL (contracts.md §11). */
export const DEFAULT_GATEWAY_BASE_URL = 'http://localhost:8080';

/** `/api/orders/{orderId}/ship` requires this header (contracts.md §2). */
export const ADMIN_ROLE_HEADER = 'X-ShopFlow-Role';
export const ADMIN_ROLE_VALUE = 'admin';
export const CORRELATION_ID_HEADER = 'X-Correlation-Id';

/**
 * Reads `VITE_GATEWAY_BASE_URL`. Read on every call so a rebuilt or reloaded
 * environment is picked up, and so tests can stub the value.
 */
export function getGatewayBaseUrl(): string {
  const configured = import.meta.env.VITE_GATEWAY_BASE_URL;
  const trimmed = typeof configured === 'string' ? configured.trim() : '';
  const base = trimmed === '' ? DEFAULT_GATEWAY_BASE_URL : trimmed;
  return base.replace(/\/+$/, '');
}

interface GatewayRequest {
  method: 'GET' | 'POST';
  path: string;
  query?: Record<string, string | number | undefined>;
  body?: unknown;
  headers?: Record<string, string>;
  correlationId?: string;
}

function buildUrl(baseUrl: string, request: GatewayRequest): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(request.query ?? {})) {
    if (value === undefined || value === null) continue;
    const text = String(value);
    if (text === '') continue;
    search.append(key, text);
  }
  const query = search.toString();
  return `${baseUrl}${request.path}${query === '' ? '' : `?${query}`}`;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function parseJson(raw: string): unknown {
  if (raw.trim() === '') return undefined;
  try {
    return JSON.parse(raw) as unknown;
  } catch {
    return undefined;
  }
}

function readErrorEnvelope(payload: unknown): ErrorEnvelope['error'] | null {
  const body = asRecord(payload);
  const error = body ? asRecord(body.error) : null;
  if (!error) return null;
  const code = error.code;
  if (typeof code !== 'string' || code === '') return null;
  return {
    code,
    message: typeof error.message === 'string' ? error.message : '',
    correlationId: typeof error.correlationId === 'string' ? error.correlationId : '',
    details: error.details,
  };
}

/** Code used when a failure did not carry a usable envelope. */
function codeFromHttpStatus(status: number): string {
  switch (status) {
    case 400:
      return 'validation_error';
    case 403:
      return 'forbidden';
    case 404:
      return 'not_found';
    case 409:
      return 'conflict';
    case 502:
    case 503:
    case 504:
      return 'upstream_unavailable';
    default:
      return status >= 500 ? UNKNOWN_ERROR_CODE : 'request_failed';
  }
}

function buildApiError(payload: unknown, response: Response, fallbackCorrelationId: string): ApiError {
  const envelope = readErrorEnvelope(payload);
  const code = envelope?.code ?? codeFromHttpStatus(response.status);
  const headerCorrelationId = response.headers.get(CORRELATION_ID_HEADER);
  const correlationId = envelope?.correlationId || headerCorrelationId || fallbackCorrelationId;

  return new ApiError({
    code,
    message: envelope?.message,
    correlationId,
    httpStatus: response.status,
    details: envelope?.details,
    retryable: isRetryableCode(code, response.status),
  });
}

async function gatewayRequest<T>(request: GatewayRequest): Promise<T> {
  const correlationId = request.correlationId ?? createCorrelationId();
  const headers: Record<string, string> = {
    Accept: 'application/json',
    [CORRELATION_ID_HEADER]: correlationId,
    ...(request.headers ?? {}),
  };

  let body: string | undefined;
  if (request.body !== undefined) {
    headers['Content-Type'] = 'application/json; charset=utf-8';
    body = JSON.stringify(request.body);
  }

  const url = buildUrl(getGatewayBaseUrl(), request);

  let response: Response;
  try {
    response = await fetch(url, { method: request.method, headers, body });
  } catch (cause) {
    throw new ApiError({
      code: NETWORK_ERROR_CODE,
      correlationId,
      httpStatus: null,
      retryable: true,
      cause,
    });
  }

  const rawBody = await response.text().catch(() => '');
  const payload = parseJson(rawBody);

  if (!response.ok) {
    throw buildApiError(payload, response, correlationId);
  }
  return payload as T;
}

/** Lists are returned as `{ items, total }`; tolerate a bare array as well. */
function asListResponse<T>(payload: unknown): ListResponse<T> {
  if (Array.isArray(payload)) {
    return { items: payload as T[], total: payload.length };
  }
  const record = asRecord(payload);
  const items = Array.isArray(record?.items) ? (record.items as T[]) : [];
  const total = typeof record?.total === 'number' ? record.total : items.length;
  return { items, total };
}

/** `GET /api/products` - catalog listing. */
export async function listProducts(params: ListProductsParams = {}): Promise<ProductListResponse> {
  const payload = await gatewayRequest<unknown>({
    method: 'GET',
    path: '/api/products',
    query: { limit: params.limit, offset: params.offset },
  });
  return asListResponse<ProductView>(payload);
}

/** `GET /api/products/{productId}` - product detail. */
export async function getProduct(productId: string): Promise<ProductView> {
  return gatewayRequest<ProductView>({
    method: 'GET',
    path: `/api/products/${encodeURIComponent(productId)}`,
  });
}

/** `POST /api/orders` - create an order with a customer id and order lines. */
export async function createOrder(request: CreateOrderRequest): Promise<OrderView> {
  return gatewayRequest<OrderView>({
    method: 'POST',
    path: '/api/orders',
    body: {
      customerId: request.customerId,
      lines: request.lines.map((line) => ({ productId: line.productId, quantity: line.quantity })),
    },
  });
}

/** `GET /api/orders/{orderId}` - order detail / status. */
export async function getOrder(orderId: string): Promise<OrderView> {
  return gatewayRequest<OrderView>({
    method: 'GET',
    path: `/api/orders/${encodeURIComponent(orderId)}`,
  });
}

/** `GET /api/orders` - order listing (optionally filtered by customer). */
export async function listOrders(params: ListOrdersParams = {}): Promise<OrderListResponse> {
  const payload = await gatewayRequest<unknown>({
    method: 'GET',
    path: '/api/orders',
    query: { customerId: params.customerId, limit: params.limit, offset: params.offset },
  });
  return asListResponse<OrderView>(payload);
}

/** `POST /api/orders/{orderId}/cancel` - cancel with a reason. */
export async function cancelOrder(orderId: string, request: CancelOrderRequest): Promise<OrderView> {
  return gatewayRequest<OrderView>({
    method: 'POST',
    path: `/api/orders/${encodeURIComponent(orderId)}/cancel`,
    body: { reason: request.reason },
  });
}

/** `POST /api/orders/{orderId}/ship` - administrative mark-as-SHIPPED. */
export async function shipOrder(orderId: string): Promise<OrderView> {
  return gatewayRequest<OrderView>({
    method: 'POST',
    path: `/api/orders/${encodeURIComponent(orderId)}/ship`,
    headers: { [ADMIN_ROLE_HEADER]: ADMIN_ROLE_VALUE },
  });
}
