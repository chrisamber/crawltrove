import { SkeletonState } from "@/components/system";

export default function Loading() {
  return (
    <main className="min-h-dvh bg-[#090a0c] px-4 py-12 text-zinc-100">
      <div className="mx-auto max-w-3xl">
        <p className="mb-4 font-mono text-[10px] uppercase tracking-[0.12em] text-zinc-600">
          CrawlTrove · Loading workspace
        </p>
        <SkeletonState />
      </div>
    </main>
  );
}
