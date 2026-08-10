import { useEffect, useState } from "react";

/** Returns `value`, delayed by `delayMs` after the last change. Used to keep
 * search-as-you-type inputs from firing a request per keystroke. */
export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [value, delayMs]);

  return debounced;
}
