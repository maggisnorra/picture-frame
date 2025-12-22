import { useEffect, useRef } from "react";
import type { PushMsg } from "../types/push";

type Opts = {
  apiBase?: string; // "" for same-origin, or "http://localhost:8000" in dev
  onMessage: (msg: PushMsg) => void;
};

export function useKioskEvents({ apiBase = "", onMessage }: Opts) {
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;

  const retryRef = useRef(0);

  useEffect(() => {
    let stopped = false;
    let es: EventSource | null = null;

    const connect = () => {
      es = new EventSource(`${apiBase}/events`);

      es.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data) as PushMsg;
          onMessage(msg);
        } catch {
          // ignore bad JSON
        }
      };

      es.onerror = () => {
        es?.close();
        if (stopped) return;
        const delay = Math.min(5000, 250 * (2 ** retryRef.current));
        retryRef.current = Math.min(retryRef.current + 1, 5);
        window.setTimeout(() => {
          if (!stopped) connect();
        }, delay);
      };

      es.onopen = () => {
        retryRef.current = 0;
      };
    };

    connect();

    return () => {
      stopped = true;
      es?.close();
      es = null;
    };
  }, [apiBase, onMessage]);
}
