import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ErrorBanner } from './ErrorBanner';
import { ApiError } from '../api/errors';

describe('ErrorBanner', () => {
  it('renders the gateway code, message and correlation id', () => {
    render(
      <ErrorBanner
        error={
          new ApiError({
            code: 'invalid_status_transition',
            message: 'order is CREATED',
            correlationId: 'corr-banner-1',
            httpStatus: 409,
          })
        }
      />,
    );

    expect(screen.getByTestId('error-code')).toHaveTextContent('invalid_status_transition');
    expect(screen.getByTestId('error-message')).toHaveTextContent(/confirmed order/i);
    expect(screen.getByTestId('error-detail')).toHaveTextContent('order is CREATED');
    expect(screen.getByTestId('error-correlation-id')).toHaveTextContent('corr-banner-1');
    expect(screen.getByRole('alert')).toHaveAttribute('data-error-code', 'invalid_status_transition');
  });

  it('offers a retry for retryable failures only', async () => {
    const onRetry = vi.fn();
    const user = userEvent.setup();

    const { unmount } = render(
      <ErrorBanner
        error={new ApiError({ code: 'network_unavailable', correlationId: 'corr-net', retryable: true })}
        onRetry={onRetry}
      />,
    );

    const retry = screen.getByTestId('error-retry');
    await user.click(retry);
    expect(onRetry).toHaveBeenCalledTimes(1);
    unmount();

    render(
      <ErrorBanner
        error={new ApiError({ code: 'order_shipped', correlationId: 'corr-shipped', httpStatus: 409 })}
        onRetry={onRetry}
      />,
    );

    expect(screen.queryByTestId('error-retry')).not.toBeInTheDocument();
    expect(screen.getByTestId('error-correlation-id')).toHaveTextContent('corr-shipped');
  });

  it('accepts an overriding message built from the error details', () => {
    render(
      <ErrorBanner
        error={new ApiError({ code: 'insufficient_stock', correlationId: 'corr-stock', details: { available: 2 } })}
        message="Not enough stock for ShopFlow Classic Tee: requested 5, only 2 available."
      />,
    );

    expect(screen.getByTestId('error-message')).toHaveTextContent('only 2 available');
  });

  it('renders nothing when there is no failure', () => {
    const { container } = render(<ErrorBanner error={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});
