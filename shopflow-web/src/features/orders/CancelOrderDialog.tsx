import { useEffect, useId, useRef, useState } from 'react';
import type { FormEvent, KeyboardEvent } from 'react';
import { cancelOrder } from '../../api/client';
import { toApiError } from '../../api/errors';
import type { ApiError } from '../../api/errors';
import { ErrorBanner } from '../../components/ErrorBanner';
import type { OrderView } from '../../api/types';

/** Server rule mirrored by the dialog (contracts.md §9, §12 `invalid_reason`). */
export const MAX_CANCELLATION_REASON_LENGTH = 200;

export interface CancelOrderDialogProps {
  orderId: string;
  onClose: () => void;
  onCancelled: (order: OrderView) => void;
}

/**
 * Cancellation dialog: `POST /api/orders/{orderId}/cancel` with `{ reason }`.
 *
 * The reason must be non-empty after trimming and at most 200 characters; both
 * rules are enforced before any request is sent. Gateway failures are mirrored
 * (`400 invalid_reason`, `409 order_shipped`, `409 order_not_cancellable`) with
 * their code and correlation id.
 */
export function CancelOrderDialog({ orderId, onClose, onCancelled }: CancelOrderDialogProps) {
  const [reason, setReason] = useState('');
  const [validationError, setValidationError] = useState<string | null>(null);
  const [serverError, setServerError] = useState<ApiError | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const reasonRef = useRef<HTMLTextAreaElement | null>(null);

  const headingId = useId();
  const counterId = useId();
  const errorId = useId();

  useEffect(() => {
    reasonRef.current?.focus();
  }, []);

  const characterCount = reason.length;
  const tooLong = characterCount > MAX_CANCELLATION_REASON_LENGTH;

  async function submitCancellation(): Promise<void> {
    if (submitting) return;

    const trimmedReason = reason.trim();
    if (trimmedReason === '') {
      setServerError(null);
      setValidationError('Enter a reason for cancelling this order.');
      return;
    }
    if (tooLong) {
      setServerError(null);
      setValidationError(
        `Shorten the reason to ${MAX_CANCELLATION_REASON_LENGTH} characters or fewer (currently ${characterCount}).`,
      );
      return;
    }

    setValidationError(null);
    setServerError(null);
    setSubmitting(true);
    try {
      const order = await cancelOrder(orderId, { reason: trimmedReason });
      onCancelled(order);
    } catch (error) {
      setServerError(toApiError(error));
    } finally {
      setSubmitting(false);
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    void submitCancellation();
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key === 'Escape') {
      event.stopPropagation();
      onClose();
    }
  }

  return (
    <div className="dialog-backdrop" onKeyDown={handleKeyDown}>
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        data-testid="cancel-order-dialog"
      >
        <h2 id={headingId}>Cancel this order</h2>
        <p className="muted" data-testid="cancel-order-id">
          Order {orderId}
        </p>

        <form className="form" onSubmit={handleSubmit} noValidate>
          <div className="field">
            <label htmlFor="cancel-reason">Cancellation reason</label>
            <textarea
              id="cancel-reason"
              name="reason"
              ref={reasonRef}
              rows={4}
              value={reason}
              onChange={(event) => {
                setReason(event.target.value);
                if (validationError !== null) setValidationError(null);
              }}
              aria-invalid={validationError !== null}
              aria-describedby={`${counterId}${validationError !== null ? ` ${errorId}` : ''}`}
            />
            <p className="counter" id={counterId} data-testid="cancel-reason-counter">
              {`${characterCount} / ${MAX_CANCELLATION_REASON_LENGTH} characters${tooLong ? ' - too long' : ''}`}
            </p>
            {validationError !== null && (
              <p className="field__error" id={errorId} role="alert" data-testid="cancel-reason-error">
                {validationError}
              </p>
            )}
          </div>

          {serverError !== null && <ErrorBanner error={serverError} />}

          <div className="dialog__actions">
            <button type="button" className="button" onClick={onClose} data-testid="dismiss-cancel-dialog">
              Keep the order
            </button>
            <button type="submit" className="button button--primary" disabled={submitting} data-testid="confirm-cancellation">
              {submitting ? 'Cancelling…' : 'Confirm cancellation'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default CancelOrderDialog;
