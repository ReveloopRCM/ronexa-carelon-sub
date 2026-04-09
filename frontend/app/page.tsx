"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { getJobStats, listWorkers } from "@/lib/api";

export default function Dashboard() {
  const [stats, setStats] = useState<any>(null);
  const [workers, setWorkers] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = async () => {
    try {
      const [s, w] = await Promise.all([getJobStats(), listWorkers()]);
      setStats(s);
      setWorkers(w);
    } catch {
      // API may not be running yet — show empty state
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 10000);
    return () => clearInterval(interval);
  }, []);

  const byStatus = stats?.by_status || {};
  const completedToday = stats?.completed_today || 0;
  const statQueued = stats?.stat_queued || 0;
  const standardQueued = stats?.standard_queued || 0;
  const exceptions = stats?.exceptions || {};
  const totalExceptions = Object.values(exceptions).reduce((a: number, b: any) => a + (b as number), 0) as number;
  const awaitingClinicals = stats?.awaiting_clinicals || 0;
  const readyForProcessing = stats?.ready_for_processing || 0;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Auth Operations Dashboard</h1>
        <span className="text-xs text-gray-400">Auto-refreshes every 10s</span>
      </div>

      {/* Top metrics row */}
      <div className="grid grid-cols-6 gap-4">
        <MetricCard
          label="Submitted Today"
          value={completedToday}
          target={1000}
          color="green"
        />
        <MetricCard
          label="STAT Queue"
          value={statQueued}
          color={statQueued > 0 ? "red" : "gray"}
          link="/queues?tab=stat"
        />
        <MetricCard
          label="Standard Queue"
          value={standardQueued}
          color="blue"
          link="/queues?tab=standard"
        />
        <MetricCard
          label="Has Clinicals"
          value={readyForProcessing}
          color={readyForProcessing > 0 ? "teal" : "gray"}
          link="/cases?state=NOTES_UPLOADED"
        />
        <MetricCard
          label="Awaiting Clinicals"
          value={awaitingClinicals}
          color={awaitingClinicals > 0 ? "purple" : "gray"}
          link="/cases?state=PENDING_NOTES"
        />
        <MetricCard
          label="Exceptions"
          value={totalExceptions}
          color={totalExceptions > 0 ? "amber" : "gray"}
          link="/worklist"
        />
      </div>

      {/* Status breakdown + Workers */}
      <div className="grid grid-cols-2 gap-6">
        {/* Job Status Breakdown */}
        <div className="border rounded-lg p-4">
          <h2 className="font-semibold mb-3">Job Status</h2>
          <div className="space-y-2">
            {[
              { key: "QUEUED", label: "Queued", color: "bg-gray-100 text-gray-700" },
              { key: "CLAIMED", label: "Claimed", color: "bg-blue-100 text-blue-700" },
              { key: "RUNNING", label: "Running", color: "bg-purple-100 text-purple-700" },
              { key: "SUSPENDED", label: "Suspended", color: "bg-amber-100 text-amber-700" },
              { key: "COMPLETED", label: "Completed", color: "bg-green-100 text-green-700" },
              { key: "FAILED", label: "Failed", color: "bg-red-100 text-red-700" },
            ].map(({ key, label, color }) => (
              <div key={key} className="flex items-center justify-between text-sm">
                <span className={`px-2 py-0.5 rounded text-xs font-medium ${color}`}>
                  {label}
                </span>
                <span className="font-mono">{byStatus[key] || 0}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Workers */}
        <div className="border rounded-lg p-4">
          <h2 className="font-semibold mb-3">Workers</h2>
          {workers.length === 0 ? (
            <p className="text-sm text-gray-400">No workers configured</p>
          ) : (
            <div className="space-y-2">
              {workers.map((w: any) => (
                <div key={w.id} className="flex items-center justify-between text-sm">
                  <div className="flex items-center gap-2">
                    <span
                      className={`w-2 h-2 rounded-full ${
                        w.is_active && w.is_logged_in
                          ? "bg-green-500"
                          : w.is_active
                          ? "bg-amber-500"
                          : "bg-gray-300"
                      }`}
                    />
                    <span className="font-medium">{w.username}</span>
                    <span className="text-xs text-gray-400">{w.shift}</span>
                    <span className={`text-xs px-1 py-0.5 rounded ${
                      w.job_type === "SUBMIT"
                        ? "bg-purple-100 text-purple-600"
                        : "bg-blue-100 text-blue-600"
                    }`}>
                      {w.job_type === "SUBMIT" ? "Submit" : "First Pass"}
                    </span>
                  </div>
                  <span className="font-mono text-xs">
                    {w.cases_today} today / {w.total_cases} total
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Exception breakdown */}
      {totalExceptions > 0 && (
        <div className="border rounded-lg p-4">
          <h2 className="font-semibold mb-3">Exception Breakdown</h2>
          <div className="grid grid-cols-3 gap-3">
            {Object.entries(exceptions).map(([type, count]) => (
              <Link
                key={type}
                href={`/worklist?type=${type}`}
                className="flex items-center justify-between border rounded p-3 hover:border-amber-400 transition"
              >
                <span className="text-sm">{formatExceptionType(type)}</span>
                <span className="bg-amber-100 text-amber-700 px-2 py-0.5 rounded text-xs font-bold">
                  {count as number}
                </span>
              </Link>
            ))}
          </div>
        </div>
      )}

      {/* Quick links */}
      <div className="grid grid-cols-3 gap-4">
        <Link
          href="/upload"
          className="border rounded-lg p-4 hover:border-blue-500 transition"
        >
          <h3 className="font-semibold mb-1">Upload</h3>
          <p className="text-xs text-gray-500">Excel files and clinical PDFs</p>
        </Link>
        <Link
          href="/cases"
          className="border rounded-lg p-4 hover:border-blue-500 transition"
        >
          <h3 className="font-semibold mb-1">All Cases</h3>
          <p className="text-xs text-gray-500">Browse, filter, and inspect</p>
        </Link>
        <Link
          href="/queue"
          className="border rounded-lg p-4 hover:border-blue-500 transition"
        >
          <h3 className="font-semibold mb-1">Review Queue</h3>
          <p className="text-xs text-gray-500">Approve AI-answered questions</p>
        </Link>
      </div>
    </div>
  );
}

function MetricCard({
  label,
  value,
  target,
  color,
  link,
}: {
  label: string;
  value: number;
  target?: number;
  color: string;
  link?: string;
}) {
  const colorMap: Record<string, string> = {
    green: "border-green-200 bg-green-50",
    red: "border-red-200 bg-red-50",
    blue: "border-blue-200 bg-blue-50",
    amber: "border-amber-200 bg-amber-50",
    purple: "border-purple-200 bg-purple-50",
    teal: "border-teal-200 bg-teal-50",
    gray: "border-gray-200 bg-gray-50",
  };
  const textMap: Record<string, string> = {
    green: "text-green-700",
    red: "text-red-700",
    blue: "text-blue-700",
    amber: "text-amber-700",
    purple: "text-purple-700",
    teal: "text-teal-700",
    gray: "text-gray-700",
  };

  const content = (
    <div className={`border rounded-lg p-4 ${colorMap[color] || colorMap.gray}`}>
      <p className="text-xs text-gray-500 mb-1">{label}</p>
      <div className="flex items-baseline gap-1">
        <span className={`text-2xl font-bold ${textMap[color] || textMap.gray}`}>
          {value}
        </span>
        {target && (
          <span className="text-sm text-gray-400">/ {target}</span>
        )}
      </div>
    </div>
  );

  if (link) {
    return <Link href={link}>{content}</Link>;
  }
  return content;
}

function formatExceptionType(type: string): string {
  const map: Record<string, string> = {
    STAT_PENDED: "STAT Pended",
    RPO_NOT_FOUND: "RPO Not Found",
    MED_NECESSITY: "Med Necessity",
    MEMBER_NOT_FOUND: "Member Not Found",
    DUPLICATE_AUTH: "Duplicate Auth",
    PORTAL_ERROR: "Portal Error",
  };
  return map[type] || type;
}
