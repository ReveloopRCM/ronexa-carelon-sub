"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import { listJobs, getJobStats } from "@/lib/api";

const STATUS_COLORS: Record<string, string> = {
  QUEUED: "bg-gray-100 text-gray-700",
  CLAIMED: "bg-blue-100 text-blue-700",
  RUNNING: "bg-purple-100 text-purple-700",
  SUSPENDED: "bg-amber-100 text-amber-700",
  COMPLETED: "bg-green-100 text-green-700",
  FAILED: "bg-red-100 text-red-700",
  CANCELLED: "bg-gray-100 text-gray-400",
};

export default function QueuesPageWrapper() {
  return (
    <Suspense fallback={<p className="text-sm text-gray-400 py-8 text-center">Loading...</p>}>
      <QueuesPage />
    </Suspense>
  );
}

function QueuesPage() {
  const searchParams = useSearchParams();
  const initialTab = searchParams.get("tab") === "standard" ? "standard" : "stat";

  const [tab, setTab] = useState<"stat" | "standard">(initialTab as any);
  const [jobs, setJobs] = useState<any[]>([]);
  const [stats, setStats] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  const refresh = async () => {
    try {
      const [jobList, s] = await Promise.all([
        listJobs({
          is_stat: tab === "stat",
          limit: 100,
        }),
        getJobStats(),
      ]);
      setJobs(jobList);
      setStats(s);
    } catch {
      // API not running
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setLoading(true);
    refresh();
    const interval = setInterval(refresh, 5000);
    return () => clearInterval(interval);
  }, [tab]);

  const statCount = stats?.stat_queued || 0;
  const standardCount = stats?.standard_queued || 0;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Submission Queues</h1>
        <span className="text-xs text-gray-400">Auto-refreshes every 5s</span>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 border-b">
        <button
          onClick={() => setTab("stat")}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition ${
            tab === "stat"
              ? "border-red-500 text-red-700"
              : "border-transparent text-gray-500 hover:text-gray-700"
          }`}
        >
          STAT / Teal
          {statCount > 0 && (
            <span className="ml-2 bg-red-100 text-red-700 px-1.5 py-0.5 rounded text-xs font-bold">
              {statCount}
            </span>
          )}
        </button>
        <button
          onClick={() => setTab("standard")}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition ${
            tab === "standard"
              ? "border-blue-500 text-blue-700"
              : "border-transparent text-gray-500 hover:text-gray-700"
          }`}
        >
          Standard
          {standardCount > 0 && (
            <span className="ml-2 bg-blue-100 text-blue-700 px-1.5 py-0.5 rounded text-xs font-bold">
              {standardCount}
            </span>
          )}
        </button>
      </div>

      {/* Table */}
      {loading ? (
        <p className="text-sm text-gray-400 py-8 text-center">Loading...</p>
      ) : jobs.length === 0 ? (
        <p className="text-sm text-gray-400 py-8 text-center">
          No {tab === "stat" ? "STAT" : "standard"} jobs in queue
        </p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-gray-500 border-b">
              <th className="pb-2 font-medium">Priority</th>
              <th className="pb-2 font-medium">Status</th>
              <th className="pb-2 font-medium">Case ID</th>
              <th className="pb-2 font-medium">Worker</th>
              <th className="pb-2 font-medium">Attempt</th>
              <th className="pb-2 font-medium">Age</th>
              <th className="pb-2 font-medium">Error</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job: any) => (
              <tr key={job.id} className="border-b hover:bg-gray-50">
                <td className="py-2">
                  <span
                    className={`px-2 py-0.5 rounded text-xs font-bold ${
                      job.priority >= 1000
                        ? "bg-red-100 text-red-700"
                        : job.priority >= 500
                        ? "bg-orange-100 text-orange-700"
                        : job.priority >= 200
                        ? "bg-amber-100 text-amber-700"
                        : "bg-gray-100 text-gray-600"
                    }`}
                  >
                    {job.priority}
                  </span>
                </td>
                <td className="py-2">
                  <span
                    className={`px-2 py-0.5 rounded text-xs font-medium ${
                      STATUS_COLORS[job.status] || STATUS_COLORS.QUEUED
                    }`}
                  >
                    {job.status}
                  </span>
                </td>
                <td className="py-2">
                  <Link
                    href={`/cases/${job.case_id}`}
                    className="text-blue-600 hover:underline font-mono text-xs"
                  >
                    {job.exam_id || job.case_id.slice(0, 8)}
                  </Link>
                </td>
                <td className="py-2 text-xs text-gray-500">
                  {job.claimed_by ? job.claimed_by.slice(0, 8) + "..." : "-"}
                </td>
                <td className="py-2 text-xs font-mono">
                  {job.attempt}/{job.max_attempts}
                </td>
                <td className="py-2 text-xs text-gray-500">
                  {job.created_at ? formatAge(job.created_at) : "-"}
                </td>
                <td className="py-2 text-xs text-red-500 max-w-[200px] truncate">
                  {job.last_error || "-"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function formatAge(isoDate: string): string {
  const diff = Date.now() - new Date(isoDate).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "<1m";
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ${mins % 60}m`;
  return `${Math.floor(hrs / 24)}d`;
}
