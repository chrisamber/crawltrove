import Link from "next/link";

import { EmptyState } from "@/components/system";
import { Button } from "@/components/ui/button";

export default function NotFound() {
  return (
    <main className="flex min-h-dvh items-center justify-center bg-[#090a0c] px-4 text-zinc-100">
      <EmptyState
        title="Workspace not found"
        description="This dashboard route does not exist. Return to the operator workspace."
        action={(
          <Button asChild size="sm" className="bg-zinc-100 text-zinc-950 hover:bg-white">
            <Link href="/">Open dashboard</Link>
          </Button>
        )}
      />
    </main>
  );
}
