"use client";

import { useEffect, useState } from "react";
import { getExecutionsSummary, getExecutionsRecent } from "@/lib/api";

const EVENT_COLORS: Record<string, string> = {
  RECEIVED: "bg-blue-100 text-blue-700",
  EXTRACTION_CLINICAL: "bg-green-100 text-green-700",
  EXTRACTION_ORDER: "bg-teal-100 text-teal-700",
  FIRST_PASS: "bg-indigo-100 text-indigo-700",
  ORDER_PASS: "bg-cyan-100 text-cyan-700",
  APPROVED: "bg-emerald-100 text-emerald-700",
  PENDED: "bg-amber-100 text-amber-700",
  DENIED: "bg-red-100 text-red-700",
  SUBMITTED: "bg-purple-100 text-purple-700",
  NO_AUTH: "bg-gray-100 text-gray-600",
};

export default function ExecutionsPage() {
  const [summary, setSummary] = useState<any>(null);
  const [recent, setRecent] = useState<any[]>([]);
  const [days, setDays] = useState(7);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    loadData();
  }, [days]);

  async function loadData() {
    setLoading(true);
    try {
      const [s, r] = await Promise.all([
        getExecutionsSummary(days),
        getExecutionsRecent(50),
      ]);
      setSummary(s);
      setRecent(r);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }

  if (loading || !summary) return <p className="text-gray-500">Loading executions...</p>;

  const t = summary.totals;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Executions</h1>
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-400">Period:</span>
          {[7, 14, 30].map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`text-xs px-2.5 py-1 rounded ${
                days === d
                  ? "bg-blue-600 text-white"
                  : "bg-gray-100 text-gray-600 hover:bg-gray-200"
              }`}
            >
              {d}d
            </button>
          ))}
        </div>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
        <SummaryCard label="Received" value={t.received} color="blue" />
        <SummaryCard label="Clinicals" value={t.processed_clinical} color="green" sub="Processed" />
        <SummaryCard label="Orders" value={t.processed_order} color="teal" sub="Processed" />
        <SummaryCard label="Approved" value={t.approved} color="emerald" />
        <SummaryCard label="Pended" value={t.pended} color="amber" />
        <SummaryCard label="Submitted" value={t.submitted} color="purple" />
      </div>

      {/* Daily Breakdown Table */}
      <div>
        <h2 className="text-lg font-semibold mb-2">Daily Breakdown</h2>
        <div className="overflow-x-auto border rounded-lg">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-gray-50 text-left">
                <th className="px-3 py-2 font-medium text-gray-600">Date</th>
                <th className="px-3 py-2 font-medium text-blue-600">Recv</th>
                <th className="px-3 py-2 font-medium text-green-600">Ext (C)</th>
                <th className="px-3 py-2 font-medium text-teal-600">Ext (O)</th>
                <th className="px-3 py-2 font-medium text-indigo-600">1st Pass</th>
                <th className="px-3 py-2 font-medium text-cyan-600">Ord Pass</th>
                <th className="px-3 py-2 font-medium text-emerald-600">Appr</th>
                <th className="px-3 py-2 font-medium text-amber-600">Pend</th>
                <th className="px-3 py-2 font-medium text-red-600">Deny</th>
                <th className="px-3 py-2 font-medium text-purple-600">Submit</th>
                <th className="px-3 py-2 font-medium text-gray-500">No Auth</th>
              </tr>
            </thead>
            <tbody>
              {[...summary.daily].reverse().map((day: any) => (
                <tr key={day.date} className="border-t hover:bg-gray-50">
                  <td className="px-3 py-2 font-mono text-xs">{day.date}</td>
                  <td className="px-3 py-2">{day.received || ""}</td>
                  <td className="px-3 py-2">{day.extraction_clinical || ""}</td>
                  <td className="px-3 py-2">{day.extraction_order || ""}</td>
                  <td className="px-3 py-2">{day.first_pass || ""}</td>
                  <td className="px-3 py-2">{day.order_pass || ""}</td>
                  <td className="px-3 py-2">{day.approved || ""}</td>
                  <td className="px-3 py-2">{day.pended || ""}</td>
                  <td className="px-3 py-2">{day.denied || ""}</td>
                  <td className="px-3 py-2">{day.submitted || ""}</td>
                  <td className="px-3 py-2">{day.no_auth || ""}</td>
                </tr>
              ))}
              {/* Totals row */}
              <tr className="border-t-2 font-semibold bg-gray-50">
                <td className="px-3 py-2">Total</td>
                <td className="px-3 py-2">{t.received}</td>
                <td className="px-3 py-2">{summary.daily.reduce((s: number, d: any) => s + (d.extraction_clinical || 0), 0)}</td>
                <td className="px-3 py-2">{summary.daily.reduce((s: number, d: any) => s + (d.extraction_order || 0), 0)}</td>
                <td className="px-3 py-2">{summary.daily.reduce((s: number, d: any) => s + (d.first_pass || 0), 0)}</td>
                <td className="px-3 py-2">{summary.daily.reduce((s: number, d: any) => s + (d.order_pass || 0), 0)}</td>
                <td className="px-3 py-2">{t.approved}</td>
                <td className="px-3 py-2">{t.pended}</td>
                <td className="px-3 py-2">{t.denied}</td>
                <td className="px-3 py-2">{t.submitted}</td>
                <td className="px-3 py-2">{t.no_auth}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      {/* Recent Activity */}
      <div>
        <h2 className="text-lg font-semibold mb-2">Recent Activity</h2>
        {recent.length === 0 ? (
          <p className="text-gray-400">No execution events recorded yet.</p>
        ) : (
          <div className="border rounded-lg overflow-hidden">
            <div className="max-h-[400px] overflow-y-auto">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 sticky top-0">
                  <tr className="text-left">
                    <th className="px-3 py-2 font-medium text-gray-600">Time</th>
                    <th className="px-3 py-2 font-medium text-gray-600">Patient</th>
                    <th className="px-3 py-2 font-medium text-gray-600">Exam</th>
                    <th className="px-3 py-2 font-medium text-gray-600">CPT</th>
                    <th className="px-3 py-2 font-medium text-gray-600">Event</th>
                    <th className="px-3 py-2 font-medium text-gray-600">Doc Type</th>
                    <th className="px-3 py-2 font-medium text-gray-600">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {recent.map((log: any) => (
                    <tr key={log.id} className="border-t hover:bg-gray-50">
                      <td className="px-3 py-2 font-mono text-xs text-gray-500">
                        {log.created_at ? new Date(log.created_at).toLocaleString() : ""}
                      </td>
                      <td className="px-3 py-2">{log.patient_name || "—"}</td>
                      <td className="px-3 py-2 text-gray-500">{log.exam_id || "—"}</td>
                      <td className="px-3 py-2">{log.cpt_code || "—"}</td>
                      <td className="px-3 py-2">
                        <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${EVENT_COLORS[log.event_type] || "bg-gray-100 text-gray-600"}`}>
                          {log.event_type}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-xs text-gray-500">
                        {log.document_type || "—"}
                      </td>
                      <td className="px-3 py-2">
                        <span className={`text-xs font-medium ${log.status === "COMPLETED" ? "text-green-600" : "text-red-600"}`}>
                          {log.status}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function SummaryCard({
  label,
  value,
  color,
  sub,
}: {
  label: string;
  value: number;
  color: string;
  sub?: string;
}) {
  const colorMap: Record<string, string> = {
    blue: "border-blue-200 bg-blue-50",
    green: "border-green-200 bg-green-50",
    teal: "border-teal-200 bg-teal-50",
    emerald: "border-emerald-200 bg-emerald-50",
    amber: "border-amber-200 bg-amber-50",
    purple: "border-purple-200 bg-purple-50",
  };
  const textMap: Record<string, string> = {
    blue: "text-blue-700",
    green: "text-green-700",
    teal: "text-teal-700",
    emerald: "text-emerald-700",
    amber: "text-amber-700",
    purple: "text-purple-700",
  };

  return (
    <div className={`border rounded-lg p-3 ${colorMap[color] || ""}`}>
      {sub && <p className="text-[10px] text-gray-400 uppercase tracking-wide">{sub}</p>}
      <p className={`text-2xl font-bold ${textMap[color] || ""}`}>{value}</p>
      <p className="text-xs text-gray-600">{label}</p>
    </div>
  );
}
