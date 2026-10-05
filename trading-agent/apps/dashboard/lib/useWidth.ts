"use client";
import { useCallback, useRef, useState } from "react";

/** Render SVG charts at their real pixel width so axis text stays legible at any card size.
 *  Callback ref: works even when the chart element mounts after the first render (data arrives late). */
export function useWidth<T extends HTMLElement>(fallback = 600) {
  const [w, setW] = useState(fallback);
  const ro = useRef<ResizeObserver | null>(null);
  const ref = useCallback((el: T | null) => {
    ro.current?.disconnect();
    if (!el) return;
    setW(Math.max(240, Math.floor(el.getBoundingClientRect().width)));
    ro.current = new ResizeObserver((es) => { for (const e of es) setW(Math.max(240, Math.floor(e.contentRect.width))); });
    ro.current.observe(el);
  }, []);
  const box = useRef<T | null>(null);
  const setRef = useCallback((el: T | null) => { box.current = el; ref(el); }, [ref]);
  return [setRef, w, box] as const;
}
