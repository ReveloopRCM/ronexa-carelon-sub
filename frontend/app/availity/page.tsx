"use client";

import { useEffect, useState } from "react";
import {
  listAvailityJobs,
  getAvailityJob,
  deleteAvailityJob,
  sendMemberNotFoundToAvaility,
  getMemberNotFoundCount,
} from "@/lib/api";

type JobStatus = "pending" | "running" | "done" | "failed";

export default function AvailityPage() {
  const [jobs, setJobs] = useState<any[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize] = useState(20);
  const [statusFilter, setStatusFilter] = useState<JobStatus | "">("");
  const [loading, setLoading] = useState(true);
  const [mnfCount, setMnfCount] = useState<{
    total: number;
    ready_to_send: number;
    skipped_incomplete: number;
  } | null>(null);
  const [sending, setSending] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [jobDetail, setJobDetail] = useState<Record<string, any>>({});
  const [toast, setToast] = useState<{ message: string; type: "success" | "error" } | null>(null);

  async function loadJobs() {
    setLoading(true);
    try {
      const data = await listAvailityJobs({
        status: statusFilter || undefined,
        page,
        page_size: pageSize,
      });
      setJobs(data.items || []);
      setTotal(data.total || 0);
    } catch (e: any) {
      setToast({ message: `Failed to load jobs: ${e.message}`, type: "error" });
    } finally {
      setLoading(false);
    }
  }

  async function loadMnfCount() {
    try {
      const data = await getMemberNotFoundCount();
      setMnfCount(data);
    } catch {
      setMnfCount(null);
    }
  }

  useEffect(() => {
    loadJobs();
  }, [page, statusFilter]);

  useEffect(() => {
    loadMnfCount();
    const t = setInterval(loadJobs, 15000); // auto-refresh job list every 15s
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 4500);
    return () => clearTimeout(t);
  }, [toast]);

  async function handleSendAll() {
    if (!mnfCount || mnfCount.ready_to_send === 0) {
      setToast({ message: "No MEMBER_NOT_FOUND cases are ready to send", type: "error" });
      return;
    }
    if (!confirm(`Send ${mnfCount.ready_to_send} Member Not Found case(s) to Availity?`)) return;
    setSending(true);
    try {
      const res = await sendMemberNotFoundToAvaility();
      const skipped = res.skipped_incomplete
        ? ` (${res.skipped_incomplete} skipped — missing fields)`
        : "";
      setToast({
        message: `Job ${res.job_id} submitted: ${res.case_count} case(s)${skipped}`,
        type: "success",
      });
      await loadJobs();
      await loadMnfCount();
    } catch (e: any) {
      setToast({ message: `Send failed: ${e.message}`, type: "error" });
    } finally {
      setSending(false);
    }
  }

  async function handleExpand(jobId: string) {
    if (expanded === jobId) {
      setExpanded(null);
      return;
    }
    setExpanded(jobId);
    if (!jobDetail[jobId]) {
      try {
        const data = await getAvailityJob(jobId);
        setJobDetail((d) => ({ ...d, [jobId]: data }));
      } catch (e: any) {
        setToast({ message: `Failed to load job: ${e.message}`, type: "error" });
      }
    }
  }

  async function handleDelete(jobId: string) {
    if (!confirm(`Delete job ${jobId}?`)) return;
    try {
      await deleteAvailityJob(jobId);
      setJobs((j) => j.filter((x) => x.job_id !== jobId));
      setToast({ message: "Job deleted", type: "success" });
    } catch (e: any) {
      setToast({ message: `Delete failed: ${e.message}`, type: "error" });
    }
  }

  const statusBadge = (s: string) => {
    const m: Record<string, string> = {
      pending: "bg-gray-100 text-gray-700",
      running: "bg-blue-100 text-blue-700",
      done: "bg-green-100 text-green-700",
      failed: "bg-red-100 text-red-700",
    };
    return m[s] || "bg-gray-100 text-gray-700";
  };

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div className="max-w-7xl mx-auto p-6">
      {toast && (
        <div
          className={`fixed top-4 right-4 z-50 px-4 py-3 rounded-lg shadow-lg text-sm text-white ${
            toast.type === "success" ? "bg-green-600" : "bg-red-600"
          }`}
        >
          {toast.message}
        </div>
      )}

      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-2xl font-semibold">Availity Eligibility Jobs</h1>
          <p className="text-sm text-gray-500 mt-1">
            Query Availity for MEMBER_NOT_FOUND cases — returns coverage + auth_org_name.
          </p>
        </div>
        <div className="flex items-center gap-3">
          {mnfCount && (
            <span className="text-sm text-gray-600">
              <strong>{mnfCount.ready_to_send}</strong> ready
              {mnfCount.skipped_incomplete > 0 && (
                <span className="text-gray-400"> · {mnfCount.skipped_incomplete} incomplete</span>
              )}
            </span>
          )}
          <button
            onClick={handleSendAll}
            disabled={sending || !mnfCount || mnfCount.ready_to_send === 0}
            className="bg-blue-600 text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50"
          >
            {sending ? "Sending..." : `Send All Member Not Found`}
          </button>
        </div>
      </div>

      <div className="mb-3 flex items-center gap-2">
        {(["", "pending", "running", "done", "failed"] as const).map((s) => (
          <button
            key={s || "all"}
            onClick={() => {
              setStatusFilter(s as any);
              setPage(1);
            }}
            className={`px-3 py-1 rounded-full text-xs ${
              statusFilter === s
                ? "bg-gray-800 text-white"
                : "bg-gray-100 text-gray-600 hover:bg-gray-200"
            }`}
          >
            {s === "" ? "All" : s.charAt(0).toUpperCase() + s.slice(1)}
          </button>
        ))}
      </div>

      <div className="border rounded-lg overflow-hidden bg-white">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 border-b text-left text-xs text-gray-500 uppercase">
            <tr>
              <th className="px-3 py-2 font-medium">Job ID</th>
              <th className="px-3 py-2 font-medium">Status</th>
              <th className="px-3 py-2 font-medium">Cases</th>
              <th className="px-3 py-2 font-medium">Processed</th>
              <th className="px-3 py-2 font-medium">Submitted</th>
              <th className="px-3 py-2 font-medium">Completed</th>
              <th className="px-3 py-2 font-medium text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={7} className="px-3 py-6 text-center text-gray-400">
                  Loading…
                </td>
              </tr>
            )}
            {!loading && jobs.length === 0 && (
              <tr>
                <td colSpan={7} className="px-3 py-6 text-center text-gray-400">
                  No jobs yet. Click "Send All Member Not Found" to submit one.
                </td>
              </tr>
            )}
            {jobs.map((j) => {
              const detail = jobDetail[j.job_id];
              const isOpen = expanded === j.job_id;
              return (
                <>
                  <tr
                    key={j.job_id}
                    className="border-b hover:bg-gray-50 cursor-pointer"
                    onClick={() => handleExpand(j.job_id)}
                  >
                    <td className="px-3 py-2 font-mono text-xs">{j.job_id?.slice(0, 8)}…</td>
                    <td className="px-3 py-2">
                      <span className={`text-xs px-2 py-0.5 rounded-full ${statusBadge(j.status)}`}>
                        {j.status}
                      </span>
                    </td>
                    <td className="px-3 py-2">{j.case_count ?? j.total ?? "-"}</td>
                    <td className="px-3 py-2">{j.processed ?? 0}</td>
                    <td className="px-3 py-2 text-xs text-gray-500">
                      {j.submitted_at ? new Date(j.submitted_at).toLocaleString() : "-"}
                    </td>
                    <td className="px-3 py-2 text-xs text-gray-500">
                      {j.completed_at ? new Date(j.completed_at).toLocaleString() : "-"}
                    </td>
                    <td className="px-3 py-2 text-right">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          handleDelete(j.job_id);
                        }}
                        className="text-xs text-red-600 hover:text-red-800"
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                  {isOpen && (
                    <tr>
                      <td colSpan={7} className="bg-gray-50 px-3 py-4">
                        {!detail ? (
                          <p className="text-xs text-gray-400">Loading details…</p>
                        ) : (
                          <div className="space-y-4">
                            {j.error && (
                              <div className="text-xs text-red-600">Error: {j.error}</div>
                            )}
                            {detail.results && detail.results.length > 0 && (
                              <ResultsTable results={detail.results} />
                            )}
                            {detail.failed_cases && detail.failed_cases.length > 0 && (
                              <FailuresTable failures={detail.failed_cases} />
                            )}
                            {(!detail.results || detail.results.length === 0) &&
                              (!detail.failed_cases || detail.failed_cases.length === 0) && (
                                <p className="text-xs text-gray-500">
                                  No results yet (job status: {detail.status}).
                                </p>
                              )}
                          </div>
                        )}
                      </td>
                    </tr>
                  )}
                </>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="mt-3 flex items-center justify-between text-sm text-gray-600">
        <span>
          Page {page} of {totalPages} · {total} total
        </span>
        <div className="flex gap-2">
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page === 1}
            className="px-3 py-1 border rounded disabled:opacity-40"
          >
            ← Prev
          </button>
          <button
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page >= totalPages}
            className="px-3 py-1 border rounded disabled:opacity-40"
          >
            Next →
          </button>
        </div>
      </div>
    </div>
  );
}

function ResultsTable({ results }: { results: any[] }) {
  return (
    <div>
      <p className="text-xs font-semibold text-gray-700 mb-2">
        Eligibility Results ({results.length})
      </p>
      <div className="overflow-x-auto border rounded bg-white">
        <table className="w-full text-xs">
          <thead className="bg-gray-50 border-b text-left text-gray-500">
            <tr>
              <th className="px-2 py-1">#</th>
              <th className="px-2 py-1">Member</th>
              <th className="px-2 py-1">Payer</th>
              <th className="px-2 py-1">Coverage</th>
              <th className="px-2 py-1">Auth Required?</th>
              <th className="px-2 py-1">Auth Org</th>
              <th className="px-2 py-1">Status</th>
            </tr>
          </thead>
          <tbody>
            {results.map((r: any, i: number) => {
              const e = r.eligibility_data || {};
              return (
                <tr key={i} className="border-b last:border-b-0">
                  <td className="px-2 py-1">{r.record_number ?? i + 1}</td>
                  <td className="px-2 py-1">
                    {e.patient_name || "?"}
                    <span className="text-gray-400 ml-1">({e.member_id || "?"})</span>
                  </td>
                  <td className="px-2 py-1">{e.payer_name || "-"}</td>
                  <td className="px-2 py-1">
                    <span
                      className={`px-1.5 py-0.5 rounded text-[10px] ${
                        (e.coverage_status || "").toLowerCase() === "active"
                          ? "bg-green-100 text-green-700"
                          : "bg-gray-100 text-gray-700"
                      }`}
                    >
                      {e.coverage_status || "-"}
                    </span>
                  </td>
                  <td className="px-2 py-1">{e.auth_status || "-"}</td>
                  <td className="px-2 py-1 font-medium">{e.auth_org_name || "-"}</td>
                  <td className="px-2 py-1">
                    <span
                      className={`px-1.5 py-0.5 rounded text-[10px] ${
                        r.status === "success"
                          ? "bg-green-100 text-green-700"
                          : "bg-red-100 text-red-700"
                      }`}
                    >
                      {r.status}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function FailuresTable({ failures }: { failures: any[] }) {
  return (
    <div>
      <p className="text-xs font-semibold text-red-700 mb-2">
        Failed Cases ({failures.length})
      </p>
      <div className="overflow-x-auto border border-red-200 rounded bg-red-50">
        <table className="w-full text-xs">
          <thead className="bg-red-100 border-b text-left text-red-700">
            <tr>
              <th className="px-2 py-1">#</th>
              <th className="px-2 py-1">Patient</th>
              <th className="px-2 py-1">Payer</th>
              <th className="px-2 py-1">Error</th>
            </tr>
          </thead>
          <tbody>
            {failures.map((f: any, i: number) => {
              const r = f.row_data || {};
              return (
                <tr key={i} className="border-b border-red-200 last:border-b-0">
                  <td className="px-2 py-1">{f.record_number ?? i + 1}</td>
                  <td className="px-2 py-1">
                    {r.FirstName} {r.LastName}
                  </td>
                  <td className="px-2 py-1">{r.Payer}</td>
                  <td className="px-2 py-1 text-red-700">{f.error}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
