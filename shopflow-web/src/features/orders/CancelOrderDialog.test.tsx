import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { CancelOrderDialog, MAX_CANCELLATION_REASON_LENGTH } from './CancelOrderDialog';
import { errorEnvelope, installGatewayMock, orderView } from '../../test/testUtils';

const ORDER_ID = '6f1c1c1e-9f1a-4a8f-9f6b-2c9a4f0d1234';

function renderDialog() {
  const onClose = vi.fn();
  const onCancelled = vi.fn();
  render(<CancelOrderDialog orderId={ORDER_ID} onClose={onClose} onCancelled={onCancelled} />);
  return { onClose, onCancelled };
}

const reasonField = () => screen.getByLabelText('Cancellation reason');
const confirmButton = () => screen.getByTestId('confirm-cancellation');

describe('CancelOrderDialog', () => {
  it('announces the limit and moves focus to the reason field', () => {
    renderDialog();

    expect(screen.getByRole('dialog')).toHaveAccessibleName('Cancel this order');
    expect(reasonField()).toHaveFocus();
    expect(screen.getByTestId('cancel-reason-counter')).toHaveTextContent(`0 / ${MAX_CANCELLATION_REASON_LENGTH} characters`);
  });

  it('refuses an empty reason without contacting the gateway', async () => {
    const mock = installGatewayMock(() => ({ body: orderView() }));
    const user = userEvent.setup();
    renderDialog();

    await user.click(confirmButton());

    expect(screen.getByTestId('cancel-reason-error')).toHaveTextContent(/enter a reason/i);
    expect(mock.requests).toHaveLength(0);
  });

  it('refuses a whitespace-only reason without contacting the gateway', async () => {
    const mock = installGatewayMock(() => ({ body: orderView() }));
    const user = userEvent.setup();
    renderDialog();

    fireEvent.change(reasonField(), { target: { value: '    ' } });
    await user.click(confirmButton());

    expect(screen.getByTestId('cancel-reason-error')).toBeInTheDocument();
    expect(mock.requests).toHaveLength(0);
  });

  it('refuses a reason longer than 200 characters before submitting', async () => {
    const mock = installGatewayMock(() => ({ body: orderView() }));
    const user = userEvent.setup();
    renderDialog();

    fireEvent.change(reasonField(), { target: { value: 'x'.repeat(201) } });
    expect(screen.getByTestId('cancel-reason-counter')).toHaveTextContent('201 / 200 characters');

    await user.click(confirmButton());

    expect(screen.getByTestId('cancel-reason-error')).toHaveTextContent(/200 characters or fewer/i);
    expect(reasonField()).toHaveAttribute('aria-invalid', 'true');
    expect(mock.requests).toHaveLength(0);
  });

  it('posts a reason of exactly 200 characters and reports the gateway answer', async () => {
    const cancelled = orderView({
      status: 'CANCELLED',
      cancelledAt: '2026-09-18T12:30:00Z',
      cancellationReason: 'y'.repeat(200),
    });
    const mock = installGatewayMock(() => ({ status: 200, body: cancelled }));
    const { onCancelled, onClose } = renderDialog();
    const user = userEvent.setup();

    const reason = 'y'.repeat(MAX_CANCELLATION_REASON_LENGTH);
    fireEvent.change(reasonField(), { target: { value: reason } });
    await user.click(confirmButton());

    expect(mock.requests).toHaveLength(1);
    expect(mock.requests[0]!.url).toBe(`http://localhost:8080/api/orders/${ORDER_ID}/cancel`);
    expect(mock.requests[0]!.body).toEqual({ reason });
    expect(onCancelled).toHaveBeenCalledWith(cancelled);
    expect(onClose).not.toHaveBeenCalled();
  });

  it('trims the reason before sending it', async () => {
    const mock = installGatewayMock(() => ({ status: 200, body: orderView({ status: 'CANCELLED' }) }));
    const { onCancelled } = renderDialog();
    const user = userEvent.setup();

    fireEvent.change(reasonField(), { target: { value: '  customer changed their mind  ' } });
    await user.click(confirmButton());

    expect(mock.requests[0]!.body).toEqual({ reason: 'customer changed their mind' });
    expect(onCancelled).toHaveBeenCalledTimes(1);
  });

  it('mirrors 409 order_shipped with the gateway message and correlation id', async () => {
    installGatewayMock(() => ({
      status: 409,
      body: errorEnvelope('order_shipped', 'order has already been shipped', undefined, 'corr-shipped'),
    }));
    const { onCancelled } = renderDialog();
    const user = userEvent.setup();

    fireEvent.change(reasonField(), { target: { value: 'customer changed their mind' } });
    await user.click(confirmButton());

    const banner = await screen.findByTestId('error-banner');
    expect(banner).toHaveAttribute('data-error-code', 'order_shipped');
    expect(screen.getByTestId('error-message').textContent).toMatch(/shipped/i);
    expect(screen.getByTestId('error-correlation-id')).toHaveTextContent('corr-shipped');
    expect(screen.queryByTestId('error-retry')).not.toBeInTheDocument();
    expect(onCancelled).not.toHaveBeenCalled();
  });

  it('mirrors 400 invalid_reason', async () => {
    installGatewayMock(() => ({
      status: 400,
      body: errorEnvelope('invalid_reason', 'reason is longer than 200 characters', undefined, 'corr-reason'),
    }));
    renderDialog();
    const user = userEvent.setup();

    fireEvent.change(reasonField(), { target: { value: 'customer changed their mind' } });
    await user.click(confirmButton());

    const banner = await screen.findByTestId('error-banner');
    expect(banner).toHaveAttribute('data-error-code', 'invalid_reason');
    expect(screen.getByTestId('error-correlation-id')).toHaveTextContent('corr-reason');
  });

  it('mirrors 409 order_not_cancellable', async () => {
    installGatewayMock(() => ({
      status: 409,
      body: errorEnvelope('order_not_cancellable', 'order is FAILED', undefined, 'corr-cancel'),
    }));
    renderDialog();
    const user = userEvent.setup();

    fireEvent.change(reasonField(), { target: { value: 'customer changed their mind' } });
    await user.click(confirmButton());

    const banner = await screen.findByTestId('error-banner');
    expect(banner).toHaveAttribute('data-error-code', 'order_not_cancellable');
    expect(screen.getByTestId('error-message').textContent).toMatch(/cancell/i);
  });

  it('keeps the dialog open on failure and closes on request', async () => {
    installGatewayMock(() => ({ status: 409, body: errorEnvelope('order_shipped') }));
    const { onClose } = renderDialog();
    const user = userEvent.setup();

    fireEvent.change(reasonField(), { target: { value: 'changed mind' } });
    await user.click(confirmButton());
    await screen.findByTestId('error-banner');
    expect(screen.getByRole('dialog')).toBeInTheDocument();

    await user.click(screen.getByTestId('dismiss-cancel-dialog'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('closes on Escape for keyboard users', async () => {
    const { onClose } = renderDialog();
    const user = userEvent.setup();

    await user.type(reasonField(), '{Escape}');

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
