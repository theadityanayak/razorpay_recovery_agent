import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Runs an async loader and tracks { data, error, loading }.
 *
 * `loader` receives an AbortSignal. In-flight requests are aborted when deps
 * change or the component unmounts, which is what stops a fast sequence of
 * filter clicks from resolving out of order and painting stale rows.
 *
 * @param {(opts: {signal: AbortSignal}) => Promise<any>} loader
 * @param {Array} deps
 * @param {{ enabled?: boolean }} options
 */
export function useApi(loader, deps = [], { enabled = true } = {}) {
  const [state, setState] = useState({
    data: null,
    error: null,
    loading: enabled,
  });
  const [nonce, setNonce] = useState(0);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  useEffect(() => {
    if (!enabled) {
      setState({ data: null, error: null, loading: false });
      return undefined;
    }

    const controller = new AbortController();
    let active = true;
    setState((prev) => ({ ...prev, loading: true, error: null }));

    loaderRef
      .current({ signal: controller.signal })
      .then((data) => {
        if (active) setState({ data, error: null, loading: false });
      })
      .catch((err) => {
        if (!active || err?.name === "AbortError") return;
        setState({ data: null, error: err, loading: false });
      });

    return () => {
      active = false;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce, enabled]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  return { ...state, reload };
}
