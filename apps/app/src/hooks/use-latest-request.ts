"use client";

import * as React from "react";

export function useLatestRequest() {
  const activeRequest = React.useRef<AbortController | null>(null);

  const cancel = React.useCallback(() => {
    activeRequest.current?.abort();
    activeRequest.current = null;
  }, []);

  React.useEffect(
    () => () => {
      cancel();
    },
    [cancel],
  );

  const start = React.useCallback(() => {
    cancel();
    const controller = new AbortController();
    activeRequest.current = controller;
    return controller;
  }, [cancel]);

  return [start, cancel] as const;
}
