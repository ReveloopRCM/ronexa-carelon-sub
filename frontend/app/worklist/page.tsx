"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import { listExceptions, resolveException, getJobStats } from "@/lib/api";

const EXCEPTION_TYPES = [
  { key: "STAT_PENDED", label: "STAT Pended", icon: "!" , color: "border-red-200 bg-red-50 text-red-700" },
  { key: "RPO_NOT_FOUND", label: "RPO Not Found", icon: "?", color: "border-orange-200 bg-orange-50 text-orange-700" },
  { key: "MED_NECESSITY", label: "Med Necessity", icon: "N", color: "border-amber-200 bg-amber-50 text-amber-700" },
  { key: "MEMBER_NOT_FOUND", label: "Member Not Found", icon: "M", color: "border-yellow-200 bg-yellow-50 text-yellow-700" },
  { key: "DUPLICATE_AUTH", label: "Duplicate Auth", icon: "D", color: "border-purple-200 bg-purple-50 text-purple-700" },
  { key: "ELIGIBILITY_EXPIRED", label: "Eligibility Expired", icon: "X", color: "border-rose-200 bg-rose-50 text-rose-700" },
  { key: "PHONE_MISSING", label: "Phone Missing", icon: "P", color: "border-cyan-200 bg-cyan-50 text-cyan-700" },
  { key: "REFERRING_NPI_MISSING", label: "Referring NPI Missing", icon: "R", color: "border-indigo-200 bg-indigo-50 text-indigo-700" },
  { key: "PORTAL_ERROR", label: "Portal Error", icon: "E", color: "border-gray-200 bg-gray-50 text-gray-700" },
];

export default function WorklistPageWrapper() {
  return (
    <Suspense fallback={<p className="text-sm text-gray-400 py-8 text-center">Loading...</p>}>
      <WorklistPage />
    </Suspense>
  );
}

function WorklistPage() {
  const searchParams = useSearchParams();
  const initialType = searchParams.get("type") || "";

  const [selectedType, setSelectedType] = useState(initialType);
  const [jobs, setJobs] = useState<any[]>([]);
  const [stats, setStats] = useState<Record<string, number>>({});
  const [selectedJob, setSelectedJob] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [resolving, setResolving] = useState(false);
  const [note, setNote] = useState("");

  const refresh = async () => {
    try {
      const [jobList, s] = await Promise.all([
        listExceptions(selectedType || undefined),
        getJobStats(),
      ]);
      setJobs(jobList);
      setStats(s?.exceptions || {});
    } catch {
      // API not running
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setLoading(true);
    setSelectedJob(null);
    refresh();
    const interval = setInterval(refresh, 10000);
    return () => clearInterval(interval);
  }, [selectedType]);

  const handleResolve = async (action: string) => {
    if (!selectedJob) return;
    setResolving(true);
    try {
      await resolveException(selectedJob.id, {
        rep_id: "rep-ui",
        action,
        note: note || undefined,
      });
      setSelectedJob(null);
      setNote("");
      refresh();
    } catch (err: any) {
      alert(`Failed: ${err.message}`);
    } finally {
      setResolving(false);
    }
  };

  const totalExceptions = Object.values(stats).reduce((a, b) => a + b, 0);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Exception Worklist</h1>
        <span className="text-sm text-gray-500">
          {totalExceptions} exception{totalExceptions !== 1 ? "s" : ""} pending
        </span>
      </div>

      <div className="flex gap-6 min-h-[500px]">
        {/* Left sidebar — exception categories */}
        <div className="w-64 flex-shrink-0 space-y-1">
          <button
            onClick={() => setSelectedType("")}
            className={`w-full text-left px-3 py-2 rounded text-sm flex items-center justify-between transition ${
              selectedType === ""
                ? "bg-gray-100 font-medium"
                : "hover:bg-gray-50"
            }`}
          >
            <span>All Exceptions</span>
            <span className="bg-gray-200 text-gray-700 px-1.5 py-0.5 rounded text-xs font-bold">
              {totalExceptions}
            </span>
          </button>

          {EXCEPTION_TYPES.map(({ key, label, color }) => {
            const count = stats[key] || 0;
            return (
              <button
                key={key}
                onClick={() => setSelectedType(key)}
                className={`w-full text-left px-3 py-2 rounded text-sm flex items-center justify-between transition ${
                  selectedType === key
                    ? "bg-gray-100 font-medium"
                    : "hover:bg-gray-50"
                }`}
              >
                <span>{label}</span>
                {count > 0 && (
                  <span className={`px-1.5 py-0.5 rounded text-xs font-bold ${color}`}>
                    {count}
                  </span>
                )}
              </button>
            );
          })}
        </div>

        {/* Right panel — job list or detail */}
        <div className="flex-1 border rounded-lg">
          {selectedJob ? (
            /* Exception detail view */
            <div className="p-4 space-y-4">
              <div className="flex items-center justify-between">
                <button
                  onClick={() => setSelectedJob(null)}
                  className="text-sm text-blue-600 hover:underline"
                >
                  Back to list
                </button>
                <span
                  className={`px-2 py-0.5 rounded text-xs font-medium ${
                    EXCEPTION_TYPES.find((e) => e.key === selectedJob.exception_type)
                      ?.color || "bg-gray-100"
                  }`}
                >
                  {EXCEPTION_TYPES.find((e) => e.key === selectedJob.exception_type)
                    ?.label || selectedJob.exception_type}
                </span>
              </div>

              <div className="border-b pb-3">
                <h2 className="font-semibold text-lg">
                  Case{" "}
                  <Link
                    href={`/cases/${selectedJob.case_id}`}
                    className="text-blue-600 hover:underline"
                  >
                    {selectedJob.exam_id || selectedJob.case_id.slice(0, 8)}
                  </Link>
                </h2>
                <p className="text-sm text-gray-600 mt-1">
                  Priority: {selectedJob.priority} | Attempt:{" "}
                  {selectedJob.attempt}/{selectedJob.max_attempts}
                </p>
              </div>

              {/* Exception detail */}
              <div className="bg-amber-50 border border-amber-200 rounded p-3">
                <p className="text-sm font-medium text-amber-800">Exception Detail</p>
                <p className="text-sm text-amber-700 mt-1">
                  {selectedJob.exception_detail || "No detail available"}
                </p>
              </div>

              {/* Action area */}
              <div className="space-y-3">
                <textarea
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  placeholder="Add a note (optional)..."
                  className="w-full border rounded p-2 text-sm h-20 resize-none"
                />

                <div className="flex gap-2">
                  {selectedJob.exception_type === "STAT_PENDED" && (
                    <>
                      <button
                        onClick={() => handleResolve("resolved")}
                        disabled={resolving}
                        className="px-4 py-2 bg-green-600 text-white rounded text-sm hover:bg-green-700 disabled:opacity-50"
                      >
                        Log Call Outcome
                      </button>
                      <button
                        onClick={() => handleResolve("skip")}
                        disabled={resolving}
                        className="px-4 py-2 bg-gray-200 text-gray-700 rounded text-sm hover:bg-gray-300 disabled:opacity-50"
                      >
                        Skip
                      </button>
                    </>
                  )}
                  {selectedJob.exception_type === "RPO_NOT_FOUND" && (
                    <>
                      <button
                        onClick={() => handleResolve("resolved")}
                        disabled={resolving}
                        className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50"
                      >
                        Provider Entered Manually
                      </button>
                      <button
                        onClick={() => handleResolve("skip")}
                        disabled={resolving}
                        className="px-4 py-2 bg-gray-200 text-gray-700 rounded text-sm hover:bg-gray-300 disabled:opacity-50"
                      >
                        Skip Case
                      </button>
                    </>
                  )}
                  {selectedJob.exception_type === "MED_NECESSITY" && (
                    <>
                      <button
                        onClick={() => handleResolve("resolved")}
                        disabled={resolving}
                        className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50"
                      >
                        Narrative Reviewed
                      </button>
                      <button
                        onClick={() => handleResolve("skip")}
                        disabled={resolving}
                        className="px-4 py-2 bg-gray-200 text-gray-700 rounded text-sm hover:bg-gray-300 disabled:opacity-50"
                      >
                        Skip
                      </button>
                    </>
                  )}
                  {selectedJob.exception_type === "MEMBER_NOT_FOUND" && (
                    <>
                      <button
                        onClick={() => handleResolve("resolved")}
                        disabled={resolving}
                        className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50"
                      >
                        Member Info Updated
                      </button>
                      <button
                        onClick={() => handleResolve("skip")}
                        disabled={resolving}
                        className="px-4 py-2 bg-gray-200 text-gray-700 rounded text-sm hover:bg-gray-300 disabled:opacity-50"
                      >
                        Route to Manual
                      </button>
                    </>
                  )}
                  {selectedJob.exception_type === "DUPLICATE_AUTH" && (
                    <>
                      <button
                        onClick={() => handleResolve("override")}
                        disabled={resolving}
                        className="px-4 py-2 bg-amber-600 text-white rounded text-sm hover:bg-amber-700 disabled:opacity-50"
                      >
                        Override & Continue
                      </button>
                      <button
                        onClick={() => handleResolve("skip")}
                        disabled={resolving}
                        className="px-4 py-2 bg-gray-200 text-gray-700 rounded text-sm hover:bg-gray-300 disabled:opacity-50"
                      >
                        Skip Case
                      </button>
                    </>
                  )}
                  {selectedJob.exception_type === "PHONE_MISSING" && (
                    <>
                      <button
                        onClick={() => handleResolve("resolved")}
                        disabled={resolving}
                        className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50"
                      >
                        Update & Retry
                      </button>
                      <button
                        onClick={() => handleResolve("skip")}
                        disabled={resolving}
                        className="px-4 py-2 bg-gray-200 text-gray-700 rounded text-sm hover:bg-gray-300 disabled:opacity-50"
                      >
                        Skip Case
                      </button>
                    </>
                  )}
                  {selectedJob.exception_type === "REFERRING_NPI_MISSING" && (
                    <>
                      <button
                        onClick={() => handleResolve("resolved")}
                        disabled={resolving}
                        className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50"
                      >
                        Update & Retry
                      </button>
                      <button
                        onClick={() => handleResolve("skip")}
                        disabled={resolving}
                        className="px-4 py-2 bg-gray-200 text-gray-700 rounded text-sm hover:bg-gray-300 disabled:opacity-50"
                      >
                        Skip Case
                      </button>
                    </>
                  )}
                  {selectedJob.exception_type === "PORTAL_ERROR" && (
                    <>
                      <button
                        onClick={() => handleResolve("resolved")}
                        disabled={resolving}
                        className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50"
                      >
                        Retry
                      </button>
                      <button
                        onClick={() => handleResolve("skip")}
                        disabled={resolving}
                        className="px-4 py-2 bg-gray-200 text-gray-700 rounded text-sm hover:bg-gray-300 disabled:opacity-50"
                      >
                        Skip
                      </button>
                    </>
                  )}
                </div>
              </div>
            </div>
          ) : (
            /* Exception list */
            <div>
              {loading ? (
                <p className="text-sm text-gray-400 py-8 text-center">Loading...</p>
              ) : jobs.length === 0 ? (
                <div className="py-12 text-center">
                  <p className="text-gray-400 text-sm">No exceptions pending</p>
                  <p className="text-gray-300 text-xs mt-1">All clear</p>
                </div>
              ) : (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs text-gray-500 border-b">
                      <th className="px-4 py-2 font-medium">Type</th>
                      <th className="py-2 font-medium">Case</th>
                      <th className="py-2 font-medium">Policy #</th>
                      <th className="py-2 font-medium">Priority</th>
                      <th className="py-2 font-medium">Detail</th>
                      <th className="py-2 font-medium">Age</th>
                    </tr>
                  </thead>
                  <tbody>
                    {jobs.map((job: any) => {
                      const excType = EXCEPTION_TYPES.find(
                        (e) => e.key === job.exception_type
                      );
                      return (
                        <tr
                          key={job.id}
                          onClick={() => setSelectedJob(job)}
                          className="border-b hover:bg-gray-50 cursor-pointer"
                        >
                          <td className="px-4 py-2">
                            <span
                              className={`px-2 py-0.5 rounded text-xs font-medium ${
                                excType?.color || "bg-gray-100"
                              }`}
                            >
                              {excType?.label || job.exception_type}
                            </span>
                          </td>
                          <td className="py-2">
                            <span className="font-mono text-xs text-blue-600">
                              {job.exam_id || job.case_id.slice(0, 8)}
                            </span>
                          </td>
                          <td className="py-2 text-xs text-gray-600 font-mono">
                            {job.policy_num || "-"}
                          </td>
                          <td className="py-2">
                            <span
                              className={`px-1.5 py-0.5 rounded text-xs font-bold ${
                                job.priority >= 1000
                                  ? "bg-red-100 text-red-700"
                                  : "bg-gray-100 text-gray-600"
                              }`}
                            >
                              {job.priority}
                            </span>
                          </td>
                          <td className="py-2 text-xs text-gray-600 max-w-[300px] truncate">
                            {job.exception_detail || "-"}
                          </td>
                          <td className="py-2 text-xs text-gray-500">
                            {job.created_at ? formatAge(job.created_at) : "-"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </div>
      </div>
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
