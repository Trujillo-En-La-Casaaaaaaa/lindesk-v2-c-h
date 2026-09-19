import { toApiError } from '../api/errors';

export interface ErrorBannerProps {
  /** Failure to render; anything that is not an ApiError is normalised. */
  error: unknown;
  /** Optional text override, used when the gateway `details` sharpen the message. */
  message?: string;
  /** When provided (and the failure is retryable) a retry button is rendered. */
  onRetry?: (() => void) | undefined;
}

/**
 * Single error surface for the whole application: it always renders the gateway
 * error `code`, a user readable `message` and the `correlationId` so a failure
 * can be traced end to end (handoff requirement 8).
 */
export function ErrorBanner({ error, message, onRetry }: ErrorBannerProps) {
  if (error === null || error === undefined) return null;

  const apiError = toApiError(error);
  const userMessage = message ?? apiError.userMessage;
  const serverDetail = apiError.serverMessage !== '' && apiError.serverMessage !== userMessage ? apiError.serverMessage : null;
  const canRetry = apiError.retryable && typeof onRetry === 'function';

  return (
    <div
      className="error-banner"
      role="alert"
      data-testid="error-banner"
      data-error-code={apiError.code}
      data-retryable={apiError.retryable ? 'true' : 'false'}
    >
      <p className="error-banner__message">
        <span className="error-banner__code" data-testid="error-code">
          {apiError.code}
        </span>
        <span data-testid="error-message">{userMessage}</span>
      </p>

      {serverDetail !== null && (
        <p className="error-banner__detail" data-testid="error-detail">
          {serverDetail}
        </p>
      )}

      <p className="error-banner__meta">
        <span>Correlation ID:</span> <code data-testid="error-correlation-id">{apiError.correlationId}</code>
      </p>

      {apiError.retryable && (
        <p className="error-banner__hint">
          This looks like a temporary failure. Nothing was confirmed by the gateway, so the request can be retried.
        </p>
      )}

      {canRetry && (
        <button type="button" className="button" onClick={onRetry} data-testid="error-retry">
          Retry
        </button>
      )}
    </div>
  );
}
