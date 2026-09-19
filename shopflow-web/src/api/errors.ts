/**
 * Error handling for the gateway API.
 *
 * Every failure surfaced to the UI is an {@link ApiError} carrying the gateway
 * error `code`, the `correlationId` and the raw `details`, so screens can branch
 * on the code (never on the HTTP status alone) and the shared banner can show a
 * traceable failure (handoff "Consistency and failure semantics").
 */

/** Code used when the gateway cannot be reached at all (no HTTP response). */
export const NETWORK_ERROR_CODE = 'network_unavailable';

/** Fallback code for a response that carries neither envelope nor known status. */
export const UNKNOWN_ERROR_CODE = 'internal_error';

const GENERIC_MESSAGE = 'The request failed. Try again, and quote the correlation id if the problem continues.';

/**
 * User facing explanation per gateway error code (contracts.md §12). Only the
 * codes the UI can actually receive are translated; unknown codes fall back to
 * the message the gateway sent.
 */
export const ERROR_MESSAGES: Readonly<Record<string, string>> = {
  validation_error: 'Some of the submitted values are invalid. Check the highlighted fields and try again.',
  invalid_reason: 'Enter a cancellation reason of 1 to 200 characters.',
  not_found: 'The requested record was not found. It may have been removed.',
  unknown_product: 'One of the requested products no longer exists.',
  forbidden: 'This action requires the admin role.',
  insufficient_stock: 'There is not enough stock to confirm this order.',
  order_shipped: 'This order has already shipped and can no longer be cancelled.',
  order_not_cancellable: 'This order cannot be cancelled in its current state.',
  invalid_status_transition: 'Only a confirmed order can be marked as shipped.',
  idempotency_conflict: 'This request conflicts with an earlier request. Refresh the page and try again.',
  inventory_unavailable: 'The inventory service is unavailable. The order was not created - try again in a moment.',
  upstream_unavailable: 'An upstream service is unavailable. Nothing was changed - try again in a moment.',
  internal_error: 'The service reported an unexpected error. Try again in a moment.',
  [NETWORK_ERROR_CODE]: 'Cannot reach the ShopFlow gateway. Check that the local stack is running, then try again.',
};

/** Codes whose failure is safe and sensible to retry (handoff, failure semantics). */
export const RETRYABLE_ERROR_CODES: ReadonlySet<string> = new Set([
  NETWORK_ERROR_CODE,
  'inventory_unavailable',
  'upstream_unavailable',
  'internal_error',
  'timeout',
]);

const RETRYABLE_STATUS_CODES: ReadonlySet<number> = new Set([429, 502, 503, 504]);

export function messageForErrorCode(code: string, fallbackMessage?: string): string {
  const translated = ERROR_MESSAGES[code];
  if (translated !== undefined) return translated;
  const fallback = typeof fallbackMessage === 'string' ? fallbackMessage.trim() : '';
  return fallback === '' ? GENERIC_MESSAGE : fallback;
}

export function isRetryableCode(code: string, httpStatus: number | null = null): boolean {
  if (RETRYABLE_ERROR_CODES.has(code)) return true;
  if (httpStatus !== null && RETRYABLE_STATUS_CODES.has(httpStatus)) return true;
  return false;
}

export interface ApiErrorInit {
  code: string;
  /** Human readable message as sent by the gateway; ignored when empty. */
  message?: string;
  correlationId: string;
  httpStatus?: number | null;
  details?: unknown;
  retryable?: boolean;
  cause?: unknown;
}

/** A gateway failure with everything needed to trace it from the browser. */
export class ApiError extends Error {
  readonly code: string;
  readonly correlationId: string;
  readonly httpStatus: number | null;
  readonly details: unknown;
  readonly retryable: boolean;
  /** Message exactly as the gateway sent it ('' when the gateway sent none). */
  readonly serverMessage: string;

  constructor(init: ApiErrorInit) {
    const serverMessage = typeof init.message === 'string' ? init.message.trim() : '';
    super(serverMessage === '' ? messageForErrorCode(init.code) : serverMessage);
    this.name = 'ApiError';
    this.code = init.code;
    this.correlationId = init.correlationId;
    this.httpStatus = init.httpStatus ?? null;
    this.details = init.details;
    this.retryable = init.retryable ?? isRetryableCode(init.code, this.httpStatus);
    this.serverMessage = serverMessage;
    if (init.cause !== undefined) {
      (this as { cause?: unknown }).cause = init.cause;
    }
  }

  /** Message to show the user: translated for known codes, gateway text otherwise. */
  get userMessage(): string {
    return messageForErrorCode(this.code, this.serverMessage);
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

export function createCorrelationId(): string {
  const cryptoApi = globalThis.crypto;
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') {
    return cryptoApi.randomUUID();
  }
  // Fallback for environments without crypto.randomUUID.
  const random = () => Math.floor(Math.random() * 0x10000).toString(16).padStart(4, '0');
  return `${random()}${random()}-${random()}-4${random().slice(1)}-a${random().slice(1)}-${random()}${random()}${random()}`;
}

/** Normalises any thrown value into an {@link ApiError} the UI can render. */
export function toApiError(value: unknown, correlationId: string = createCorrelationId()): ApiError {
  if (isApiError(value)) return value;
  if (value instanceof Error) {
    return new ApiError({
      code: UNKNOWN_ERROR_CODE,
      message: value.message,
      correlationId,
      retryable: false,
      cause: value,
    });
  }
  return new ApiError({
    code: UNKNOWN_ERROR_CODE,
    message: typeof value === 'string' ? value : undefined,
    correlationId,
    retryable: false,
  });
}

export function errorCodeOf(value: unknown): string | null {
  return isApiError(value) ? value.code : null;
}

export function hasErrorCode(value: unknown, code: string): boolean {
  return isApiError(value) && value.code === code;
}

/** Form fields the UI can point at when the gateway reports `validation_error`. */
export type FormFieldName = 'customerId' | 'quantity' | 'productId' | 'reason' | 'lines';

const KNOWN_FIELDS: ReadonlySet<string> = new Set<FormFieldName>([
  'customerId',
  'quantity',
  'productId',
  'reason',
  'lines',
]);

export type FieldErrors = Partial<Record<FormFieldName, string>>;

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asText(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : undefined;
}

function asNumber(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

/**
 * Extracts field level messages from a `validation_error` envelope.
 *
 * The gateway forwards the owning service `details` untouched and the exact
 * inner shape is private to that service, so several common shapes are
 * tolerated: `{ fields: { quantity: "..." } }`, `{ errors: [{ field, message }] }`,
 * `{ field, message }`, arrays and flat `{ quantity: "..." }` maps. Unknown
 * fields are ignored.
 */
export function extractFieldErrors(error: unknown): FieldErrors {
  if (!isApiError(error)) return {};
  const result: FieldErrors = {};
  const record = (field: string, message: string) => {
    if (KNOWN_FIELDS.has(field) && !(field in result)) {
      result[field as FormFieldName] = message;
    }
  };

  const walk = (raw: unknown, depth = 0): void => {
    if (raw === null || raw === undefined || depth > 4) return;
    if (Array.isArray(raw)) {
      for (const item of raw) walk(item, depth + 1);
      return;
    }
    const value = asRecord(raw);
    if (!value) return;

    const field = asText(value.field) ?? asText(value.path) ?? asText(value.name);
    const message = asText(value.message) ?? asText(value.error) ?? asText(value.detail);
    if (field && message) {
      record(field, message);
      return;
    }

    for (const [key, entry] of Object.entries(value)) {
      const text = asText(entry);
      if (text !== undefined) {
        record(key, text);
        continue;
      }
      if (Array.isArray(entry) || asRecord(entry)) walk(entry, depth + 1);
    }
  };

  walk(error.details);
  return result;
}

/** Collects human readable messages from `details` that are not field specific. */
export function extractDetailMessages(error: unknown): string[] {
  if (!isApiError(error)) return [];
  const messages: string[] = [];
  const record = asRecord(error.details);
  if (!record) return messages;
  for (const [key, entry] of Object.entries(record)) {
    if (key === 'fields' || key === 'errors' || key === 'items' || key === 'lines') continue;
    const text = asText(entry);
    if (text !== undefined) messages.push(text);
  }
  return messages;
}

interface StockShortfall {
  productId?: string;
  sku?: string;
  requested?: number;
  available?: number;
}

function firstShortfall(details: unknown): StockShortfall {
  const candidates: unknown[] = [];
  if (Array.isArray(details)) {
    candidates.push(...details);
  } else {
    const record = asRecord(details);
    if (record) {
      if (Array.isArray(record.items)) candidates.push(...record.items);
      if (Array.isArray(record.lines)) candidates.push(...record.lines);
      candidates.push(record);
    }
  }

  for (const candidate of candidates) {
    const record = asRecord(candidate);
    if (!record) continue;
    const shortfall: StockShortfall = {
      productId: asText(record.productId),
      sku: asText(record.sku),
      requested: asNumber(record.requested) ?? asNumber(record.requestedQuantity),
      available: asNumber(record.available) ?? asNumber(record.availableQuantity),
    };
    if (
      shortfall.productId !== undefined ||
      shortfall.sku !== undefined ||
      shortfall.requested !== undefined ||
      shortfall.available !== undefined
    ) {
      return shortfall;
    }
  }
  return {};
}

/**
 * Builds the `409 insufficient_stock` message from the error `details` so the
 * screen can state what was requested and what is actually available.
 */
export function insufficientStockMessage(
  error: unknown,
  context: { productName?: string | undefined; productId?: string | undefined } = {},
): string {
  const apiError = toApiError(error);
  const shortfall = firstShortfall(apiError.details);
  const name = context.productName ?? shortfall.sku ?? shortfall.productId ?? context.productId;

  let quantity: string | undefined;
  if (shortfall.requested !== undefined && shortfall.available !== undefined) {
    quantity = `requested ${shortfall.requested}, only ${shortfall.available} available`;
  } else if (shortfall.available !== undefined) {
    quantity = `only ${shortfall.available} available`;
  }

  if (quantity !== undefined) {
    return name !== undefined
      ? `Not enough stock for ${name}: ${quantity}. Reduce the quantity and submit again.`
      : `Not enough stock: ${quantity}. Reduce the quantity and submit again.`;
  }
  if (apiError.serverMessage !== '') return apiError.serverMessage;
  return messageForErrorCode('insufficient_stock');
}
