import { describe, expect, it } from 'vitest';
import {
  ApiError,
  createCorrelationId,
  errorCodeOf,
  extractFieldErrors,
  hasErrorCode,
  insufficientStockMessage,
  isApiError,
  isRetryableCode,
  messageForErrorCode,
  toApiError,
} from './errors';

describe('api/errors', () => {
  it('translates every gateway error code the UI can receive (contracts.md §12)', () => {
    const codes = [
      'validation_error',
      'invalid_reason',
      'not_found',
      'unknown_product',
      'forbidden',
      'insufficient_stock',
      'order_shipped',
      'order_not_cancellable',
      'invalid_status_transition',
      'idempotency_conflict',
      'inventory_unavailable',
      'upstream_unavailable',
      'internal_error',
      'network_unavailable',
    ];

    for (const code of codes) {
      const message = messageForErrorCode(code);
      expect(message, code).not.toBe('');
      expect(message, code).not.toBe(messageForErrorCode('unknown-code-without-fallback'));
    }
  });

  it('describes the shipped-order guard in user language', () => {
    expect(messageForErrorCode('order_shipped')).toMatch(/shipped/i);
    expect(messageForErrorCode('order_shipped')).toMatch(/cancell/i);
    expect(messageForErrorCode('order_not_cancellable')).toMatch(/cancell/i);
    expect(messageForErrorCode('invalid_reason')).toMatch(/200/);
    expect(messageForErrorCode('insufficient_stock')).toMatch(/stock/i);
    expect(messageForErrorCode('invalid_status_transition')).toMatch(/confirmed/i);
  });

  it('marks upstream and transport failures retryable and user errors not retryable', () => {
    expect(isRetryableCode('network_unavailable')).toBe(true);
    expect(isRetryableCode('upstream_unavailable')).toBe(true);
    expect(isRetryableCode('inventory_unavailable')).toBe(true);
    expect(isRetryableCode('unknown', 503)).toBe(true);
    expect(isRetryableCode('insufficient_stock', 409)).toBe(false);
    expect(isRetryableCode('order_shipped', 409)).toBe(false);
    expect(isRetryableCode('validation_error', 400)).toBe(false);
  });

  it('keeps the gateway message, code, correlation id and details', () => {
    const error = new ApiError({
      code: 'insufficient_stock',
      message: 'requested 99, available 50',
      correlationId: 'corr-99',
      httpStatus: 409,
      details: { productId: 'p-1001', requested: 99, available: 50 },
    });

    expect(error).toBeInstanceOf(Error);
    expect(isApiError(error)).toBe(true);
    expect(error.code).toBe('insufficient_stock');
    expect(error.correlationId).toBe('corr-99');
    expect(error.httpStatus).toBe(409);
    expect(error.retryable).toBe(false);
    expect(error.message).toBe('requested 99, available 50');
    expect(error.userMessage).toBe(messageForErrorCode('insufficient_stock'));
    expect(errorCodeOf(error)).toBe('insufficient_stock');
    expect(hasErrorCode(error, 'insufficient_stock')).toBe(true);
    expect(hasErrorCode(error, 'order_shipped')).toBe(false);
  });

  it('falls back to the gateway message for codes it does not translate', () => {
    const error = new ApiError({ code: 'brand_new_code', message: 'Something specific', correlationId: 'corr-1' });
    expect(error.userMessage).toBe('Something specific');
  });

  it('normalises arbitrary thrown values and always carries a correlation id', () => {
    const fromError = toApiError(new Error('boom'));
    expect(fromError.code).toBe('internal_error');
    expect(fromError.correlationId).not.toBe('');
    expect(toApiError('plain text').message).toBe('plain text');
    expect(toApiError(42).retryable).toBe(false);

    const created = createCorrelationId();
    expect(typeof created).toBe('string');
    expect(created.length).toBeGreaterThan(8);
    expect(createCorrelationId()).not.toBe(created);
  });

  it('extracts field level messages from several validation details shapes', () => {
    const nested = toApiError(
      new ApiError({
        code: 'validation_error',
        correlationId: 'c',
        details: { fields: { quantity: 'must be greater than 0', ignored: 'x' } },
      }),
    );
    expect(extractFieldErrors(nested)).toEqual({ quantity: 'must be greater than 0' });

    const listed = toApiError(
      new ApiError({
        code: 'validation_error',
        correlationId: 'c',
        details: { errors: [{ field: 'customerId', message: 'is required' }] },
      }),
    );
    expect(extractFieldErrors(listed)).toEqual({ customerId: 'is required' });

    const single = toApiError(
      new ApiError({
        code: 'validation_error',
        correlationId: 'c',
        details: { field: 'quantity', message: 'must be a positive integer' },
      }),
    );
    expect(extractFieldErrors(single)).toEqual({ quantity: 'must be a positive integer' });

    const array = toApiError(
      new ApiError({
        code: 'validation_error',
        correlationId: 'c',
        details: [{ path: 'lines', message: 'at least one line is required' }],
      }),
    );
    expect(extractFieldErrors(array)).toEqual({ lines: 'at least one line is required' });

    expect(extractFieldErrors(new Error('not an api error'))).toEqual({});
    expect(extractFieldErrors(toApiError(new ApiError({ code: 'validation_error', correlationId: 'c' })))).toEqual({});
  });

  it('builds the insufficient stock message from the error details', () => {
    const flat = toApiError(
      new ApiError({
        code: 'insufficient_stock',
        message: 'not enough stock',
        correlationId: 'corr-1',
        details: { productId: 'p-1001', requested: 99, available: 50 },
      }),
    );
    expect(insufficientStockMessage(flat)).toContain('requested 99');
    expect(insufficientStockMessage(flat)).toContain('50');

    const withName = insufficientStockMessage(flat, { productName: 'ShopFlow Classic Tee' });
    expect(withName).toContain('ShopFlow Classic Tee');
    expect(withName).toContain('requested 99, only 50 available');

    const items = toApiError(
      new ApiError({
        code: 'insufficient_stock',
        correlationId: 'corr-2',
        details: { items: [{ productId: 'p-1005', requested: 2, available: 0, sufficient: false }] },
      }),
    );
    expect(insufficientStockMessage(items)).toContain('only 0 available');

    const availableOnly = toApiError(
      new ApiError({ code: 'insufficient_stock', correlationId: 'corr-3', details: { availableQuantity: 3 } }),
    );
    expect(insufficientStockMessage(availableOnly)).toContain('only 3 available');

    const noDetails = toApiError(new ApiError({ code: 'insufficient_stock', correlationId: 'corr-4', message: '' }));
    expect(insufficientStockMessage(noDetails)).toBe(messageForErrorCode('insufficient_stock'));

    const serverOnly = toApiError(
      new ApiError({ code: 'insufficient_stock', correlationId: 'corr-5', message: 'only 2 left for SF-TEE-001' }),
    );
    expect(insufficientStockMessage(serverOnly)).toBe('only 2 left for SF-TEE-001');
  });
});
