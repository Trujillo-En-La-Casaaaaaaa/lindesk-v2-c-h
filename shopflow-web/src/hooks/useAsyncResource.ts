import { useCallback, useEffect, useRef, useState } from 'react';
import { toApiError } from '../api/errors';
import type { ApiError } from '../api/errors';

export interface AsyncResource<T> {
  /** Last value returned by the gateway, or null while it has never loaded. */
  data: T | null;
  /** Last failure, mapped from the gateway error envelope. */
  error: ApiError | null;
  /** True while a request is in flight. */
  loading: boolean;
  /** Re-runs the loader (used by refresh buttons and retry banners). */
  reload: () => void;
  /** Replaces the value with a fresh gateway response. */
  replace: (data: T | null) => void;
}

/**
 * Loads a resource from the gateway and keeps the screen in sync with the last
 * gateway response. Nothing is invented locally: values only ever come from a
 * gateway reply (handoff "Consistency and failure semantics").
 */
export function useAsyncResource<T>(load: () => Promise<T>, deps: readonly unknown[]): AsyncResource<T> {
  const [state, setState] = useState<{ data: T | null; error: ApiError | null; loading: boolean }>({
    data: null,
    error: null,
    loading: true,
  });

  const latestRequest = useRef(0);
  const loadRef = useRef(load);
  loadRef.current = load;

  const reload = useCallback(() => {
    const requestId = latestRequest.current + 1;
    latestRequest.current = requestId;
    setState((previous) => ({ data: previous.data, error: null, loading: true }));

    loadRef.current().then(
      (data) => {
        if (latestRequest.current === requestId) setState({ data, error: null, loading: false });
      },
      (error: unknown) => {
        if (latestRequest.current === requestId) {
          setState((previous) => ({ data: previous.data, error: toApiError(error), loading: false }));
        }
      },
    );
  }, []);

  useEffect(() => {
    reload();
  }, deps);

  const replace = useCallback((data: T | null) => {
    latestRequest.current += 1;
    setState({ data, error: null, loading: false });
  }, []);

  return { data: state.data, error: state.error, loading: state.loading, reload, replace };
}
