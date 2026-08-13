/** Motion system - PRINCIPAL-OWNED. Built on motion.dev's vanilla `animate`.
 *
 * Governance (MASTER.md v2.1 "Motion"): this workspace is data-dense and
 * analytical, so motion exists to mark *orientation changes* (arriving on a
 * page, a surface mounting), never to decorate data. Three sanctioned
 * patterns, all defined here:
 *
 *  1. Route entrance - `useRouteEntrance`: opacity-only fade of the routed
 *     content. Opacity only, deliberately: a `transform` on <main> would
 *     make it a containing block and silently re-anchor every
 *     `position: fixed` drawer rendered inside it.
 *  2. Staggered surface entrance - `useEntranceStagger`: card-level elements
 *     fade up 10px, 60ms apart. Mount-time only; refetches never animate.
 *  3. Button press - CSS-only (`:active` transforms in the stylesheets);
 *     no JS involvement.
 *
 * Everything is inert when the user prefers reduced motion, and in
 * environments without real animation support (jsdom in tests): elements
 * simply render in their final state.
 */

// "motion/mini": the WAAPI-only build (a few KB instead of the full hybrid
// engine's ~60KB - which measurably regressed the code-split main chunk).
// `motionEnabled()` below already requires WAAPI, so mini loses nothing here.
import { animate } from "motion/mini";
import { stagger } from "motion";
import { useLayoutEffect, useRef } from "react";

/** True only where animating is both possible and welcome. */
export function motionEnabled(): boolean {
  if (typeof window === "undefined") return false;
  if (typeof window.matchMedia !== "function") return false; // jsdom: stay inert
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return false;
  return typeof Element !== "undefined" && "animate" in Element.prototype;
}

const EASE_OUT: [number, number, number, number] = [0.22, 1, 0.36, 1];

/**
 * Staggered fade-up entrance for the children matching `selector` inside the
 * returned container ref. Runs once per mount (not per render, not per
 * refetch); inert under reduced motion.
 */
export function useEntranceStagger<T extends HTMLElement>(selector: string): React.RefObject<T> {
  const ref = useRef<T>(null);
  useLayoutEffect(() => {
    if (!motionEnabled() || !ref.current) return;
    // Skip display:none elements (e.g. the login thesis hidden at <=900px):
    // WAAPI's commitStyles throws "Target element is not rendered" for them,
    // and an exception in this effect's cleanup tears down the React tree.
    const items = Array.from(ref.current.querySelectorAll<HTMLElement>(selector)).filter(
      (el) => el.getClientRects().length > 0,
    );
    if (items.length === 0) return;
    const controls = animate(
      items,
      // mini is a WAAPI wrapper: plain CSS properties, no `y` shorthand
      { opacity: [0, 1], transform: ["translateY(10px)", "translateY(0px)"] },
      { duration: 0.35, delay: stagger(0.06), ease: EASE_OUT },
    );
    return () => {
      try {
        controls.stop();
      } catch {
        // an element hidden mid-animation must never crash an unmount
      }
    };
    // mount-only by design; the selector is a static literal at every call site
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return ref;
}

/**
 * Opacity-only entrance re-run on every `key` change (pass the current
 * pathname). Safe to attach to <main>: no transform is ever applied.
 */
export function useRouteEntrance<T extends HTMLElement>(key: string): React.RefObject<T> {
  const ref = useRef<T>(null);
  useLayoutEffect(() => {
    if (!motionEnabled() || !ref.current) return;
    if (ref.current.getClientRects().length === 0) return; // not rendered: skip
    const controls = animate(ref.current, { opacity: [0, 1] }, { duration: 0.22, ease: "easeOut" });
    return () => {
      try {
        controls.stop();
      } catch {
        // an element hidden mid-animation must never crash an unmount
      }
    };
  }, [key]);
  return ref;
}
