"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { listCases, getCaseCounts, syncFromMongo } from "@/lib/api";

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
  // v155 — Call Worklist: physician must initiate, rep needs to phone the
  // physician's office. Distinct visual from NO_AUTH (emerald = done) and
  // HOLD (amber = error); orange-red signals "needs your phone".
  PHYSICIAN_CALL_REQUIRED: "bg-orange-200 text-orange-900",
  ALREADY_WORKED: "bg-gray-100 text-gray-500",
  APPROVED: "bg-green-100 text-green-700",
  DENIED: "bg-red-100 text-red-700",
  PENDED: "bg-amber-100 text-amber-700",
  FAILED: "bg-red-200 text-red-800",
  SUBMISSION_ERROR: "bg-rose-100 text-rose-700",
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
const SUBMISSION_ERROR_STATES = ["SUBMISSION_ERROR"];
const HOLD_STATES = ["HOLD"];
// v155 — Call Worklist: physician-initiation-required cases live here, NOT
// in Completed. Imaging center cannot submit; rep must call physician.
const CALL_STATES = ["PHYSICIAN_CALL_REQUIRED"];
const COMPLETED_STATES = ["APPROVED", "DENIED", "PENDED", "NO_AUTH_REQUIRED", "ALREADY_WORKED"];

// Date default pinned to Central time so the picker never rolls to
// tomorrow's UTC date while Chicago is still on today. `en-CA` locale
// yields a YYYY-MM-DD string the backend's date_from/date_to expects.
function todayInChicago(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Chicago",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

type Tab = "all_active" | "submission" | "submission_error" | "hold" | "call" | "completed";

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

// Page size for paginated tabs (Completed / Hold / Submission / Submission Errors).
// All Active tab uses a much larger fetch (LIMIT_ACTIVE) since reps need to
// see the entire active backlog filtered by bucket pills client-side.
const PAGE_SIZE = 50;
const LIMIT_ACTIVE = 2000;

// Sum case counts across a list of states. Drives tab badges from the
// /api/cases/counts response.
function sumStates(counts: Record<string, number>, states: string[]): number {
  return states.reduce((acc, s) => acc + (counts[s] || 0), 0);
}

export default function CasesPage() {
  // Currently-displayed rows. For paginated tabs this is one page (≤PAGE_SIZE);
  // for all_active it's the full active dataset (≤LIMIT_ACTIVE) which is then
  // bucket-filtered client-side.
  const [activeCases, setActiveCases] = useState<any[]>([]);
  const [pagedCases, setPagedCases] = useState<any[]>([]);
  const [pagedTotal, setPagedTotal] = useState(0);

  const [counts, setCounts] = useState<Record<string, number>>({});
  const [tab, setTab] = useState<Tab>("all_active");
  const [activeBucket, setActiveBucket] = useState("all");
  const [stateFilter, setStateFilter] = useState("");
  const [dateFilter, setDateFilter] = useState(todayInChicago());
  const [page, setPage] = useState(0); // zero-indexed
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [syncResult, setSyncResult] = useState<any>(null);

  // Re-fetch counts on date change (tab badges).
  useEffect(() => {
    loadCounts();
  }, [dateFilter]);

  // Re-fetch current view on tab / page / state-filter / date / bucket change.
  // For all_active we re-fetch the entire active dataset; for paginated tabs
  // we fetch just the current page.
  useEffect(() => {
    loadCases();
  }, [tab, page, stateFilter, dateFilter]);

  // Reset page → 0 whenever tab changes or filters change (so we don't land
  // on page 5 of a different tab's empty result set).
  useEffect(() => {
    setPage(0);
  }, [tab, stateFilter, dateFilter]);

  async function loadCounts() {
    try {
      const result = await getCaseCounts({
        date_from: dateFilter,
        date_to: dateFilter,
      });
      setCounts(result.counts_by_state || {});
    } catch (err) {
      console.error(err);
    }
  }

  async function loadCases() {
    setLoading(true);
    try {
      if (tab === "all_active") {
        // Full active backlog — bucket pills filter client-side.
        const data = await listCases({
          state: ALL_ACTIVE_STATES.join(","),
          order_by: "priority",
          limit: LIMIT_ACTIVE,
          date_from: dateFilter,
          date_to: dateFilter,
        });
        setActiveCases(data.items);
        setPagedCases([]);
        setPagedTotal(0);
      } else {
        // Per-tab paginated fetch with most-recent-first ordering.
        let stateParam: string;
        if (tab === "completed") {
          stateParam = stateFilter || COMPLETED_STATES.join(",");
        } else if (tab === "submission") {
          stateParam = SUBMISSION_STATES.join(",");
        } else if (tab === "submission_error") {
          stateParam = SUBMISSION_ERROR_STATES.join(",");
        } else if (tab === "hold") {
          stateParam = HOLD_STATES.join(",");
        } else if (tab === "call") {
          stateParam = CALL_STATES.join(",");
        } else {
          stateParam = "";
        }
        const data = await listCases({
          state: stateParam,
          order_by: "recent",
          limit: PAGE_SIZE,
          offset: page * PAGE_SIZE,
          date_from: dateFilter,
          date_to: dateFilter,
        });
        setPagedCases(data.items);
        setPagedTotal(data.total);
        setActiveCases([]);
      }
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }

  // Counts for tab badges — derived from the /counts endpoint, which gives
  // accurate per-state totals regardless of the current page.
  const activeCount = sumStates(counts, ALL_ACTIVE_STATES);
  const submissionCount = sumStates(counts, SUBMISSION_STATES);
  const submissionErrorCount = sumStates(counts, SUBMISSION_ERROR_STATES);
  const holdCount = sumStates(counts, HOLD_STATES);
  const callCount = sumStates(counts, CALL_STATES);
  const completedCount = sumStates(counts, COMPLETED_STATES);

  // Bucket pill counts — applied to the active dataset, not /counts, because
  // some buckets gate on `is_stat` (a row-level field, not a state). Only
  // meaningful when the all_active tab is loaded.
  const bucketCounts = BUCKETS.reduce((acc, b) => {
    acc[b.key] = activeCases.filter(b.filter).length;
    return acc;
  }, {} as Record<string, number>);

  // Compute the rows to render in the table for the current tab.
  const cases =
    tab === "all_active"
      ? (() => {
          const bucket = BUCKETS.find((b) => b.key === activeBucket);
          return bucket
            ? activeCases.filter(bucket.filter)
            : activeCases.filter((c: any) => ALL_ACTIVE_STATES.includes(c.state));
        })()
      : pagedCases;

  // Pagination control values (only meaningful for paginated tabs).
  const totalPages = tab === "all_active" ? 1 : Math.max(1, Math.ceil(pagedTotal / PAGE_SIZE));
  const showingFrom = pagedTotal === 0 ? 0 : page * PAGE_SIZE + 1;
  const showingTo = Math.min((page + 1) * PAGE_SIZE, pagedTotal);

  const tabs: { key: Tab; label: string; count: number; color: string; activeColor: string }[] = [
    { key: "all_active", label: "All Active", count: activeCount, color: "bg-blue-100 text-blue-700", activeColor: "border-blue-600 text-blue-600" },
    { key: "submission", label: "Submission", count: submissionCount, color: "bg-cyan-100 text-cyan-700", activeColor: "border-cyan-600 text-cyan-600" },
    { key: "submission_error", label: "Submission Errors", count: submissionErrorCount, color: "bg-rose-100 text-rose-700", activeColor: "border-rose-600 text-rose-600" },
    { key: "hold", label: "On Hold", count: holdCount, color: "bg-amber-100 text-amber-700", activeColor: "border-amber-600 text-amber-600" },
    { key: "call", label: "Call Worklist", count: callCount, color: "bg-orange-200 text-orange-900", activeColor: "border-orange-600 text-orange-700" },
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
                await Promise.all([loadCases(), loadCounts()]);
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

      {/* 5-Tab Navigation */}
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

        {/* Filters: date picker is global (applies to every tab); the
            completed-tab outcome dropdown narrows further within COMPLETED_STATES. */}
        <div className="ml-auto flex items-center gap-2">
          <input
            type="date"
            value={dateFilter}
            onChange={(e) => setDateFilter(e.target.value)}
            className="border rounded px-3 py-1.5 text-sm"
          />
          {tab === "completed" && (
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
          )}
        </div>
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
            : tab === "call"
            ? "No physician-call cases — all caught up."
            : "No active cases found."}
        </p>
      ) : tab === "call" ? (
        /* ── Call Worklist Tab (v155) ──
           Cases where the portal said "treating physician must initiate
           the Carelon Order Request". Imaging center cannot submit; rep
           needs to phone the referring physician's office. */
        <div className="overflow-x-auto">
          <table className="w-full text-sm border">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-3 py-2 text-left">ExamId</th>
                <th className="px-3 py-2 text-left">Patient</th>
                <th className="px-3 py-2 text-left">DOB</th>
                <th className="px-3 py-2 text-left">CPT</th>
                <th className="px-3 py-2 text-left">Center</th>
                <th className="px-3 py-2 text-left">Portal Message</th>
                <th className="px-3 py-2 text-left">Flagged</th>
              </tr>
            </thead>
            <tbody>
              {cases.map((c) => (
                <tr key={c.id} className="border-t hover:bg-orange-50">
                  <td className="px-3 py-2">
                    <Link
                      href={`/cases/${c.id}`}
                      className="font-mono text-xs text-blue-600 hover:underline"
                    >
                      {c.exam_id}
                    </Link>
                  </td>
                  <td className="px-3 py-2 font-medium">
                    {c.first_name} {c.last_name}
                  </td>
                  <td className="px-3 py-2 text-xs text-gray-600">{c.dob || "—"}</td>
                  <td className="px-3 py-2">
                    <div>{c.cpt_code}</div>
                    {(c.body_side_desc || c.body_part_desc) && (
                      <div className="text-[11px] text-gray-500 mt-0.5">
                        {[c.body_side_desc, c.body_part_desc].filter(Boolean).join(" ")}
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2">{c.center_abbr || c.center_npi}</td>
                  <td className="px-3 py-2 text-xs text-orange-900 max-w-[360px]">
                    {c.hold_reason || "Physician initiation required"}
                  </td>
                  <td className="px-3 py-2 text-xs text-gray-500">
                    {c.updated_at
                      ? new Date(c.updated_at).toLocaleString()
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
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
                  <td className="px-3 py-2">
                    <div>{c.cpt_code}</div>
                    {(c.body_side_desc || c.body_part_desc) && (
                      <div className="text-[11px] text-gray-500 mt-0.5">
                        {[c.body_side_desc, c.body_part_desc].filter(Boolean).join(" ")}
                      </div>
                    )}
                  </td>
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
                  <td className="px-3 py-2">
                    <div>{c.cpt_code}</div>
                    {(c.body_side_desc || c.body_part_desc) && (
                      <div className="text-[11px] text-gray-500 mt-0.5">
                        {[c.body_side_desc, c.body_part_desc].filter(Boolean).join(" ")}
                      </div>
                    )}
                  </td>
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
                  <td className="px-3 py-2">
                    <div>{c.cpt_code}</div>
                    {(c.body_side_desc || c.body_part_desc) && (
                      <div className="text-[11px] text-gray-500 mt-0.5">
                        {[c.body_side_desc, c.body_part_desc].filter(Boolean).join(" ")}
                      </div>
                    )}
                  </td>
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
                  <td className="px-3 py-2">
                    <div>{c.cpt_code}</div>
                    {(c.body_side_desc || c.body_part_desc) && (
                      <div className="text-[11px] text-gray-500 mt-0.5">
                        {[c.body_side_desc, c.body_part_desc].filter(Boolean).join(" ")}
                      </div>
                    )}
                  </td>
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

      {/* Pagination footer \u2014 only on paginated tabs (not all_active). The
          all_active tab loads up to LIMIT_ACTIVE rows in one shot since
          bucket pills filter client-side and need the full dataset. */}
      {tab !== "all_active" && pagedTotal > 0 && (
        <div className="flex items-center justify-between text-sm text-gray-600 pt-2 border-t">
          <div>
            Showing <span className="font-medium">{showingFrom}</span>\u2013
            <span className="font-medium">{showingTo}</span> of{" "}
            <span className="font-medium">{pagedTotal}</span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              disabled={page === 0}
              className="px-3 py-1 border rounded text-sm disabled:opacity-40 hover:bg-gray-50"
            >
              \u2190 Prev
            </button>
            <span className="text-xs">
              Page {page + 1} of {totalPages}
            </span>
            <button
              onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
              disabled={page >= totalPages - 1}
              className="px-3 py-1 border rounded text-sm disabled:opacity-40 hover:bg-gray-50"
            >
              Next \u2192
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
