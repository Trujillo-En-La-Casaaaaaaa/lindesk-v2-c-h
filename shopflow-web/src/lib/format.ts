/** Display helpers. No formatting decision changes any business value. */

/** Formats a monetary amount for display; amounts always come from the gateway. */
export function formatMoney(amount: number | null | undefined, currency = 'USD'): string {
  const value = typeof amount === 'number' && Number.isFinite(amount) ? amount : 0;
  const code = typeof currency === 'string' && currency.trim() !== '' ? currency.trim() : 'USD';
  try {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: code }).format(value);
  } catch {
    return `${value.toFixed(2)} ${code}`;
  }
}

/**
 * Timestamps are shown exactly as the gateway returned them (ISO-8601 UTC, for
 * example `2026-09-18T12:00:00Z`) so a failure or cancellation can be traced.
 */
export function formatTimestamp(value: string | null | undefined): string {
  return typeof value === 'string' && value.trim() !== '' ? value : '';
}
