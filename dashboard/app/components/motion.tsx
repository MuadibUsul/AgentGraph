"use client";

import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type PointerEvent,
  type RefObject,
} from "react";

/*
 * Small motion helpers in the spirit of Cult UI (animated numbers, sliding
 * tab indicators, spotlight cards) without a motion library. Every helper
 * respects prefers-reduced-motion and renders the final state on the server.
 */

function reducedMotion(): boolean {
  return (
    typeof window === "undefined" ||
    window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true
  );
}

/** Counts from the previous value to the new one (ease-out, ~600 ms). */
export function AnimatedNumber({
  value,
  format = (n) => n.toLocaleString(),
  duration = 600,
}: {
  value: number;
  format?: (value: number) => string;
  duration?: number;
}) {
  const [shown, setShown] = useState(value);
  const from = useRef(value);

  useEffect(() => {
    const start = from.current;
    if (start === value || reducedMotion()) {
      from.current = value;
      setShown(value);
      return;
    }
    let frame = 0;
    const began = performance.now();
    const tick = (now: number) => {
      const progress = Math.min(1, (now - began) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      setShown(Math.round(start + (value - start) * eased));
      if (progress < 1) {
        frame = requestAnimationFrame(tick);
      } else {
        from.current = value;
      }
    };
    frame = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(frame);
      from.current = value;
    };
  }, [value, duration]);

  return <span className="tabular">{format(shown)}</span>;
}

const useIsoLayoutEffect = typeof window === "undefined" ? useEffect : useLayoutEffect;

/**
 * Position of a sliding indicator under the active child of `container`
 * (the element marked `aria-selected="true"` or `.active`). It re-measures
 * after every render, since the container may mount late, and on resize or
 * once web fonts load; state only changes when the box moves.
 */
export function useSlidingIndicator(
  container: RefObject<HTMLElement | null>,
  activeKey: string | number,
): { left: number; width: number; ready: boolean } {
  const [box, setBox] = useState({ left: 0, width: 0, ready: false });
  const measure = useRef<() => void>(() => undefined);

  measure.current = () => {
    const active = container.current?.querySelector<HTMLElement>(
      '[aria-selected="true"], .active',
    );
    if (!active) {
      if (box.ready) setBox({ left: 0, width: 0, ready: false });
      return;
    }
    const next = { left: active.offsetLeft, width: active.offsetWidth, ready: true };
    if (next.left !== box.left || next.width !== box.width || !box.ready) setBox(next);
  };

  useIsoLayoutEffect(() => {
    measure.current();
  });

  useEffect(() => {
    const onResize = () => measure.current();
    window.addEventListener("resize", onResize);
    void document.fonts?.ready.then(onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [activeKey]);

  return box;
}

/** Pointer handler that lets a card's spotlight follow the cursor. */
export function spotlight(event: PointerEvent<HTMLElement>) {
  const target = event.currentTarget;
  const rect = target.getBoundingClientRect();
  target.style.setProperty("--spot-x", `${event.clientX - rect.left}px`);
  target.style.setProperty("--spot-y", `${event.clientY - rect.top}px`);
}
