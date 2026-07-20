"use client";

import * as React from "react";

import { ErrorState } from "@/components/system";

export default function ErrorPage({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  React.useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <main className="flex min-h-dvh items-center justify-center bg-[#090a0c] px-4 text-zinc-100">
      <ErrorState
        title="Dashboard unavailable"
        description="The interface hit an unexpected error. Retry the view; FastAPI data and active backend jobs are unaffected."
        onRetry={reset}
      />
    </main>
  );
}
