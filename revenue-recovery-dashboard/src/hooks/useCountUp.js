import { useEffect, useRef, useState } from "react";

const prefersReducedMotion = () =>
  typeof window !== "undefined" &&
  window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/**
 * Eases a number up to `target` on change.
 *
 * The count-up is the one piece of non-user-triggered motion in the product:
 * it runs once when the stats land, to draw the eye to the header. It is
 * skipped entirely under prefers-reduced-motion, which returns the final
 * value immediately rather than animating faster.
 */
export function useCountUp(target, { duration = 900 } = {}) {
  const [value, setValue] = useState(target ?? 0);
  const fromRef = useRef(0);
  const frameRef = useRef(0);

  useEffect(() => {
    if (target === null || target === undefined || Number.isNaN(target)) return undefined;

    if (prefersReducedMotion()) {
      setValue(target);
      fromRef.current = target;
      return undefined;
    }

    const from = fromRef.current;
    const delta = target - from;
    if (delta === 0) return undefined;

    const start = performance.now();
    // easeOutExpo: fast off the mark, long settle — reads as a counter
    // resolving rather than a bar filling.
    const ease = (t) => (t === 1 ? 1 : 1 - Math.pow(2, -10 * t));

    const tick = (now) => {
      const t = Math.min(1, (now - start) / duration);
      setValue(from + delta * ease(t));
      if (t < 1) frameRef.current = requestAnimationFrame(tick);
      else fromRef.current = target;
    };

    frameRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frameRef.current);
  }, [target, duration]);

  return value;
}
