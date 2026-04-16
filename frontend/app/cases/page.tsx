"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { listCases, syncFromMongo } from "@/lib/api";

const STATE_COLORS: Record<string, string> = {
  PENDING_NOTES: "bg-gray-100 text-gray-700",
  PENDING_STAT: "bg-red-100 text-red-700",
  HOLD: "bg-amber-100 text-amber-700",
  NOTES_UPLOADED: "bg-blue-100 text-blue-700",
  PROCESSING: "bg-purple-100 text-purple-700",
  L1_REVIEW: "bg-indigo-100 text-indigo-700",
  L2_REVIEW: "bg-indigo-100 text-indigo-700",
  APPROVED_FOR_SUBMIT: "bg-green-100 text-green-700",
  SUBMITTING: "bg-cyan-100 text-cyan-700",
  IN_REVIEW: "bg-indigo-100 text-indigo-700",
  WAITING_CLINICALS: "bg-yellow-100 text-yellow-700",
  PENDED_FAX_REVIEW: "bg-orange-100 text-orange-700",
  NO_AUTH_REQUIRED: "bg-emerald-100 text-emerald-700",
  ALREADY_WORKED: "bg-gray-100 text-gray-500",
  APPROVED: "bg-green-100 text-green-700",
  DENIED: "bg-red-100 text-red-700",
  PENDED: "bg-amber-100 text-amber-700",
  FAILED: "bg-red-200 text-red-800",
};

// ── Tab Definitions ──
// Each tab defines which case states it shows.

const ALL_ACTIVE_STATES = [
  "PENDING_NOTES",
  "PENDING_STAT",
  "NOTES_UPLOADED",
  "PROCESSING",
  "IN_REVIEW",
  "L1_REVIEW",
  "L2_REVIEW",
  "APPROVED_FOR_SUBMIT",
  "SUBMITTING",
  "WAITING_CLINICALS",
  "PENDED_FAX_REVIEW",
  "FAILED",
];

const SUBMISSION_STATES = ["SUBMITTING"];
const HOLD_STATES = ["HOLD"];
const COMPLETED_STATES = ["APPROVED", "DENIED", "PENDED", "NO_AUTH_REQUIRED", "ALREADY_WORKED"];

type Tab = "all_active" | "submission" | "hold" | "completed";

// Bucket pills for the "All Active" tab — quick filters within active cases
const BUCKETS = [
  { key: "all", label: "All Active", filter: (c: any) => ALL_ACTIVE_STATES.includes(c.state) },
  { key: "ready", label: "Has Clinicals", color: "bg-teal-100 text-teal-700", filter: (c: any) => c.state === "NOTES_UPLOADED" },
  { key: "awaiting", label: "Awaiting Clinicals", color: "bg-purple-100 text-purple-700", filter: (c: any) => ["PENDING_NOTES", "WAITING_CLINICALS"].includes(c.state) && !c.is_stat },
  { key: "stat", label: "STAT Ready", color: "bg-red-100 text-red-700", filter: (c: any) => c.is_stat && c.state === "NOTES_UPLOADED" },
  { key: "stat_awaiting", label: "STAT Awaiting", color: "bg-red-50 text-red-600 border border-red-200", filter: (c: any) => c.is_stat && ["PENDING_NOTES", "PENDING_STAT", "WAITING_CLINICALS"].includes(c.state) },
  { key: "processing", label: "In Progress", color: "bg-blue-100 text-blue-700", filter: (c: any) => c.state === "PROCESSING" },
  { key: "review", label: "In Review", color: "bg-indigo-100 text-indigo-700", filter: (c: any) => ["L1_REVIEW", "L2_REVIEW", "IN_REVIEW", "PENDED_FAX_REVIEW"].includes(c.state) },
  { key: "failed", label: "Failed", color: "bg-red-100 text-red-700", filter: (c: any) => c.state === "FAILED" },
];

export default function CasesPage() {
  const [allCases, setAllCases] = useState<any[]>([]);
  const [cases, setCases] = useState<any[]>([]);
  const [tab, setTab] = useState<Tab>("all_active");
  const [activeBucket, setActiveBucket] = useState("all");
  const [stateFilter, setStateFilter] = useState("");
  const [dateFilter, setDateFilter] = useState(new Date().toISOString().slice(0, 10));
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [syncResult, setSyncResult] = useState<any>(null);

  useEffect(() => {
    loadCases();
  }, []);

  // Re-filter when tab, bucket, or stateFilter changes
  useEffect(() => {
    if (tab === "all_active") {
      const bucket = BUCKETS.find((b) => b.key === activeBucket);
      const filtered = bucket
        ? allCases.filter(bucket.filter)
        : allCases.filter((c: any) => ALL_ACTIVE_STATES.includes(c.state));
      setCases(filtered);
    } else if (tab === "submission") {
      setCases(allCases.filter((c: any) => SUBMISSION_STATES.includes(c.state)));
    } else if (tab === "hold") {
      setCases(allCases.filter((c: any) => HOLD_STATES.includes(c.state)));
    } else if (tab === "completed") {
      const filtered = stateFilter
        ? allCases.filter((c: any) => c.state === stateFilter)
        : allCases.filter((c: any) => COMPLETED_STATES.includes(c.state));
      setCases(filtered);
    }
  }, [allCases, tab, activeBucket, stateFilter]);

  async function loadCases(dateOverride?: string) {
    setLoading(true);
    try {
      const d = dateOverride ?? dateFilter;
      const data = await listCases({
        limit: 500,
        ...(d ? { date_from: d, date_to: d } : {}),
      });
      setAllCases(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }

  // Counts per tab
  const activeCount = allCases.filter((c: any) => ALL_ACTIVE_STATES.includes(c.state)).length;
  const submissionCount = allCases.filter((c: any) => SUBMISSION_STATES.includes(c.state)).length;
  const holdCount = allCases.filter((c: any) => HOLD_STATES.includes(c.state)).length;
  const completedCount = allCases.filter((c: any) => COMPLETED_STATES.includes(c.state)).length;
  const bucketCounts = BUCKETS.reduce((acc, b) => {
    acc[b.key] = allCases.filter(b.filter).length;
    return acc;
  }, {} as Record<string, number>);

  const tabs: { key: Tab; label: string; count: number; color: string; activeColor: string }[] = [
    { key: "all_active", label: "All Active", count: activeCount, color: "bg-blue-100 text-blue-700", activeColor: "border-blue-600 text-blue-600" },
    { key: "submission", label: "Submission", count: submissionCount, color: "bg-cyan-100 text-cyan-700", activeColor: "border-cyan-600 text-cyan-600" },
    { key: "hold", label: "On Hold", count: holdCount, color: "bg-amber-100 text-amber-700", activeColor: "border-amber-600 text-amber-600" },
    { key: "completed", label: "Completed", count: completedCount, color: "bg-green-100 text-green-700", activeColor: "border-green-600 text-green-600" },
  ];

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Cases</h1>
        <div className="flex items-center gap-3">
          <button
            onClick={async () => {
              setSyncing(true);
              setSyncResult(null);
              try {
                const result = await syncFromMongo({ extract: false });
                setSyncResult(result);
                await loadCases();
              } catch (err: any) {
                setSyncResult({ error: err.message });
              } finally {
                setSyncing(false);
              }
            }}
            disabled={syncing}
            className="bg-green-600 text-white px-3 py-1.5 rounded text-sm hover:bg-green-700 disabled:opacity-50"
          >
            {syncing ? "Syncing..." : "Sync from Portal"}
          </button>
        </div>
      </div>

      {syncResult && (
        <div
          className={`text-sm rounded p-3 ${
            syncResult.error
              ? "bg-red-50 text-red-700"
              : "bg-green-50 text-green-700"
          }`}
        >
          {syncResult.error
            ? `Sync failed: ${syncResult.error}`
            : `Synced ${syncResult.new_cases} new case${
                syncResult.new_cases !== 1 ? "s" : ""
              } (${syncResult.duplicates_skipped} skipped, ${
                syncResult.total_fetched
              } total in queue)`}
        </div>
      )}

      {/* 4-Tab Navigation */}
      <div className="flex items-center gap-0 border-b">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => {
              setTab(t.key);
              setActiveBucket("all");
              setStateFilter("");
            }}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition ${
              tab === t.key
                ? t.activeColor
                : "border-transparent text-gray-500 hover:text-gray-700"
            }`}
          >
            {t.label}
            {t.count > 0 && (
              <span
                className={`ml-1.5 text-xs px-1.5 py-0.5 rounded-full ${
                  tab === t.key ? t.color : "bg-gray-100 text-gray-600"
                }`}
              >
                {t.count}
              </span>
            )}
          </button>
        ))}

        {/* Filters for Completed tab */}
        {tab === "completed" && (
          <div className="ml-auto flex items-center gap-2">
            <input
              type="date"
              value={dateFilter}
              onChange={(e) => {
                setDateFilter(e.target.value);
                loadCases(e.target.value);
              }}
              className="border rounded px-3 py-1.5 text-sm"
            />
            <select
              value={stateFilter}
              onChange={(e) => setStateFilter(e.target.value)}
              className="border rounded px-3 py-1.5 text-sm"
            >
              <option value="">All outcomes</option>
              {COMPLETED_STATES.map((s) => (
                <option key={s} value={s}>
                  {s.replace(/_/g, " ")}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>

      {/* Bucket pills (All Active tab only) */}
      {tab === "all_active" && (
        <div className="flex flex-wrap gap-2">
          {BUCKETS.map((b) => {
            const count = bucketCounts[b.key] || 0;
            if (count === 0 && b.key !== "all") return null;
            const isActive = activeBucket === b.key;
            return (
              <button
                key={b.key}
                onClick={() => setActiveBucket(b.key)}
                className={`px-3 py-1.5 rounded-full text-xs font-medium transition ${
                  isActive
                    ? b.color || "bg-blue-100 text-blue-700"
                    : "bg-gray-100 text-gray-500 hover:bg-gray-200"
                }`}
              >
                {b.label}
                <span className="ml-1.5 font-mono">{count}</span>
              </button>
            );
          })}
        </div>
      )}

      {/* Table */}
      {loading ? (
        <p className="text-gray-500">Loading...</p>
      ) : cases.length === 0 ? (
        <p className="text-gray-500">
          {tab === "completed"
            ? "No completed cases yet."
            : tab === "submission"
            ? "No cases in submission queue."
            : tab === "hold"
            ? "No cases on hold."
            : "No active cases found."}
        </p>
      ) : tab === "completed" ? (
        /* ── Completed Tab ── */
        <div className="overflow-x-auto">
          <table className="w-full text-sm border">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-3 py-2 text-left">Outcome</th>
                <th className="px-3 py-2 text-left">ExamId</th>
                <th className="px-3 py-2 text-left">Patient</th>
                <th className="px-3 py-2 text-left">CPT</th>
                <th className="px-3 py-2 text-left">Center</th>
                <th className="px-3 py-2 text-left">Auth #</th>
                <th className="px-3 py-2 text-left">Valid Through</th>
                <th className="px-3 py-2 text-left">Details</th>
                <th className="px-3 py-2 text-left">Submitted</th>
              </tr>
            </thead>
            <tbody>
              {cases.map((c) => (
                <tr key={c.id} className="border-t hover:bg-gray-50">
                  <td className="px-3 py-2">
                    <span
                      className={`text-xs font-medium px-2 py-0.5 rounded ${
                        STATE_COLORS[c.state] || "bg-gray-100"
                      }`}
                    >
                      {c.state}
                    </span>
                  </td>
                  <td className="px-3 py-2">
                    <Link
                      href={`/cases/${c.id}`}
                      className="font-mono text-xs text-blue-600 hover:underline"
                    >
                      {c.exam_id}
                    </Link>
                  </td>
                  <td className="px-3 py-2">
                    {c.first_name} {c.last_name}
                  </td>
                  <td className="px-3 py-2">{c.cpt_code}</td>
                  <td className="px-3 py-2">{c.center_abbr || c.center_npi}</td>
                  <td className="px-3 py-2">
                    {c.auth_number ? (
                      <span className="font-mono text-xs bg-green-50 text-green-800 px-1.5 py-0.5 rounded">
                        {c.auth_number}
                      </span>
                    ) : (
                      <span className="text-gray-400">&mdash;</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-xs text-gray-500">
                    {c.valid_through || "\u2014"}
                  </td>
                  <td className="px-3 py-2 text-xs text-gray-600 max-w-[200px] truncate">
                    {c.state === "DENIED" && c.denial_reason
                      ? c.denial_reason
                      : c.state === "PENDED" && c.pend_reason
                      ? c.pend_reason
                      : c.determination_status || "\u2014"}
                  </td>
                  <td className="px-3 py-2 text-xs text-gray-500">
                    {c.submitted_at
                      ? new Date(c.submitted_at).toLocaleString()
                      : c.updated_at
                      ? new Date(c.updated_at).toLocaleString()
                      : "\u2014"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : tab === "hold" ? (
        /* ── On Hold Tab ── */
        <div className="overflow-x-auto">
          <table className="w-full text-sm border">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-3 py-2 text-left">State</th>
                <th className="px-3 py-2 text-left">ExamId</th>
                <th className="px-3 py-2 text-left">Patient</th>
                <th className="px-3 py-2 text-left">CPT</th>
                <th className="px-3 py-2 text-left">Center</th>
                <th className="px-3 py-2 text-left">Hold Reason</th>
                <th className="px-3 py-2 text-left">Updated</th>
              </tr>
            </thead>
            <tbody>
              {cases.map((c) => (
                <tr key={c.id} className="border-t hover:bg-gray-50">
                  <td className="px-3 py-2">
                    <span
                      className={`text-xs font-medium px-2 py-0.5 rounded ${
                        STATE_COLORS[c.state] || "bg-gray-100"
                      }`}
                    >
                      {c.state}
                    </span>
                  </td>
                  <td className="px-3 py-2">
                    <Link
                      href={`/cases/${c.id}`}
                      className="font-mono text-xs text-blue-600 hover:underline"
                    >
                      {c.exam_id}
                    </Link>
                  </td>
                  <td className="px-3 py-2">
                    {c.first_name} {c.last_name}
                  </td>
                  <td className="px-3 py-2">{c.cpt_code}</td>
                  <td className="px-3 py-2">{c.center_abbr || c.center_npi}</td>
                  <td className="px-3 py-2 text-xs text-amber-700 max-w-[300px]">
                    {c.hold_reason || c.exception_detail || "\u2014"}
                  </td>
                  <td className="px-3 py-2 text-xs text-gray-500">
                    {c.updated_at
                      ? new Date(c.updated_at).toLocaleString()
                      : "\u2014"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : tab === "submission" ? (
        /* ── Submission Tab ── */
        <div className="overflow-x-auto">
          <table className="w-full text-sm border">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-3 py-2 text-left">State</th>
                <th className="px-3 py-2 text-left">ExamId</th>
                <th className="px-3 py-2 text-left">Patient</th>
                <th className="px-3 py-2 text-left">CPT</th>
                <th className="px-3 py-2 text-left">ICD</th>
                <th className="px-3 py-2 text-left">Center</th>
                <th className="px-3 py-2 text-left">Updated</th>
              </tr>
            </thead>
            <tbody>
              {cases.map((c) => (
                <tr key={c.id} className="border-t hover:bg-gray-50">
                  <td className="px-3 py-2">
                    <span
                      className={`text-xs font-medium px-2 py-0.5 rounded ${
                        STATE_COLORS[c.state] || "bg-gray-100"
                      }`}
                    >
                      {c.state}
                    </span>
                  </td>
                  <td className="px-3 py-2">
                    <Link
                      href={`/cases/${c.id}`}
                      className="font-mono text-xs text-blue-600 hover:underline"
                    >
                      {c.exam_id}
                    </Link>
                  </td>
                  <td className="px-3 py-2">
                    {c.first_name} {c.last_name}
                  </td>
                  <td className="px-3 py-2">{c.cpt_code}</td>
                  <td className="px-3 py-2 text-xs">{c.icd1}</td>
                  <td className="px-3 py-2">{c.center_abbr || c.center_npi}</td>
                  <td className="px-3 py-2 text-xs text-gray-500">
                    {c.updated_at
                      ? new Date(c.updated_at).toLocaleString()
                      : "\u2014"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        /* ── All Active Tab ── */
        <div className="overflow-x-auto">
          <table className="w-full text-sm border">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-3 py-2 text-left">State</th>
                <th className="px-3 py-2 text-left">ExamId</th>
                <th className="px-3 py-2 text-left">Patient</th>
                <th className="px-3 py-2 text-left">CPT</th>
                <th className="px-3 py-2 text-left">Center</th>
                <th className="px-3 py-2 text-left">Auth #</th>
                <th className="px-3 py-2 text-left">Updated</th>
              </tr>
            </thead>
            <tbody>
              {cases.map((c) => (
                <tr key={c.id} className="border-t hover:bg-gray-50">
                  <td className="px-3 py-2">
                    <span
                      className={`text-xs px-2 py-0.5 rounded ${
                        STATE_COLORS[c.state] || "bg-gray-100"
                      }`}
                    >
                      {c.state}
                    </span>
                    {c.state === "HOLD" && c.hold_reason && (
                      <span className="text-xs text-amber-600 ml-2">
                        {c.hold_reason}
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <Link
                      href={`/cases/${c.id}`}
                      className="font-mono text-xs text-blue-600 hover:underline"
                    >
                      {c.exam_id}
                    </Link>
                  </td>
                  <td className="px-3 py-2">
                    {c.first_name} {c.last_name}
                  </td>
                  <td className="px-3 py-2">{c.cpt_code}</td>
                  <td className="px-3 py-2">{c.center_abbr || c.center_npi}</td>
                  <td className="px-3 py-2 font-mono text-xs">
                    {c.auth_number || "\u2014"}
                  </td>
                  <td className="px-3 py-2 text-xs text-gray-500">
                    {c.updated_at
                      ? new Date(c.updated_at).toLocaleString()
                      : "\u2014"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
