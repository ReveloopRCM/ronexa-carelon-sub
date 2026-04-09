"use client";

import { useEffect, useState, useCallback } from "react";
import Link from "next/link";
import {
  getAnalyticsOverview,
  getAnalyticsOverrides,
  getAnalyticsOutcomes,
  getAnalyticsCoverage,
  getAnalyticsApprovalBreakdown,
  getAnalyticsPathwayIntelligence,
  getAnalyticsSubmissionSignatures,
} from "@/lib/api";

// ── Tab Definitions ──

const TABS = [
  { key: "overview", label: "Overview" },
  { key: "approval", label: "Approval Breakdown" },
  { key: "pathways", label: "Pathway Intelligence" },
  { key: "outcomes", label: "Outcome Signal" },
  { key: "quality", label: "Answer Quality" },
  { key: "signatures", label: "Signatures" },
  { key: "coverage", label: "Coverage" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

export default function AnalyticsPage() {
  const [tab, setTab] = useState<TabKey>("overview");

  // Data cache — each endpoint fetched once on first visit to its tab
  const [overview, setOverview] = useState<any>(null);
  const [overrides, setOverrides] = useState<any>(null);
  const [outcomes, setOutcomes] = useState<any>(null);
  const [coverage, setCoverage] = useState<any>(null);
  const [approvalBreakdown, setApprovalBreakdown] = useState<any>(null);
  const [pathwayIntel, setPathwayIntel] = useState<any>(null);
  const [signatures, setSignatures] = useState<any>(null);
  const [tabLoading, setTabLoading] = useState(false);

  // Track which tabs have been fetched
  const [fetched, setFetched] = useState<Set<TabKey>>(new Set());

  const fetchTab = useCallback(async (t: TabKey) => {
    if (fetched.has(t)) return;
    setTabLoading(true);
    try {
      switch (t) {
        case "overview":
          const [ov, cov] = await Promise.all([
            getAnalyticsOverview().catch(() => null),
            getAnalyticsCoverage().catch(() => null),
          ]);
          setOverview(ov);
          setCoverage(cov);
          break;
        case "approval":
          setApprovalBreakdown(await getAnalyticsApprovalBreakdown().catch(() => null));
          break;
        case "pathways":
          setPathwayIntel(await getAnalyticsPathwayIntelligence().catch(() => null));
          break;
        case "outcomes":
          setOutcomes(await getAnalyticsOutcomes().catch(() => null));
          break;
        case "quality":
          setOverrides(await getAnalyticsOverrides().catch(() => null));
          break;
        case "signatures":
          setSignatures(await getAnalyticsSubmissionSignatures().catch(() => null));
          break;
        case "coverage":
          if (!coverage) setCoverage(await getAnalyticsCoverage().catch(() => null));
          break;
      }
    } finally {
      setFetched((prev) => new Set(prev).add(t));
      setTabLoading(false);
    }
  }, [fetched, coverage]);

  // Fetch data when tab changes
  useEffect(() => {
    fetchTab(tab);
  }, [tab, fetchTab]);

  const ov = overview || {};
  const tiers = coverage?.coverage_tiers || {};
  const abTotals = approvalBreakdown?.totals || {};

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Data Moat Analytics</h1>
        <span className="text-xs text-gray-400">
          Three feedback loops compounding over time
        </span>
      </div>

      {/* Tab Bar */}
      <div className="flex gap-1 border-b pb-0">
        {TABS.map(({ key, label }) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`px-4 py-2 text-sm font-medium rounded-t-lg border-b-2 transition-colors ${
              tab === key
                ? "border-blue-500 text-blue-700 bg-blue-50"
                : "border-transparent text-gray-500 hover:text-gray-700 hover:bg-gray-50"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Tab Loading */}
      {tabLoading && (
        <p className="text-sm text-gray-400 py-4 text-center">Loading...</p>
      )}

      {/* ═══ Overview Tab ═══ */}
      {tab === "overview" && !tabLoading && (
        <div className="space-y-6">
          {/* Dataset Overview Cards */}
          <div className="grid grid-cols-4 gap-4">
            <StatCard label="Labeled Outcomes" value={ov.total_outcomes || 0} sub="outcome_patterns rows" color="blue" />
            <StatCard label="Rep Overrides" value={ov.total_rep_overrides || 0} sub={`${((ov.override_rate || 0) * 100).toFixed(1)}% override rate`} color="amber" />
            <StatCard label="Cases Completed" value={ov.total_cases_completed || 0} sub={`${ov.unique_cpt_codes || 0} CPT codes`} color="green" />
            <StatCard label="CPT x ICD Combos" value={ov.unique_cpt_icd_combos || 0} sub={`${tiers["100+"] || 0} deep (100+)`} color="purple" />
          </div>

          {/* Coverage Tiers Bar */}
          <div className="border rounded-lg p-4">
            <h2 className="font-semibold mb-3">Pattern Coverage Depth</h2>
            <div className="flex gap-2 items-end h-16">
              {[
                { tier: "100+", color: "bg-green-500", label: "Deep" },
                { tier: "50-99", color: "bg-blue-500", label: "Strong" },
                { tier: "10-49", color: "bg-amber-500", label: "Growing" },
                { tier: "1-9", color: "bg-gray-300", label: "Thin" },
              ].map(({ tier, color, label }) => {
                const count = tiers[tier] || 0;
                const total = Object.values(tiers).reduce((a: number, b: any) => a + (b as number), 0) as number;
                const pct = total > 0 ? (count / total) * 100 : 0;
                return (
                  <div key={tier} className="flex-1 flex flex-col items-center">
                    <div className={`w-full ${color} rounded-t`} style={{ height: `${Math.max(pct, 4)}%`, minHeight: 4 }} />
                    <span className="text-xs text-gray-500 mt-1">{label}</span>
                    <span className="text-xs font-mono">{count}</span>
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      )}

      {/* ═══ Approval Breakdown Tab ═══ */}
      {tab === "approval" && !tabLoading && (
        <div className="space-y-4">
          {/* 5 Colored Stat Cards */}
          <div className="grid grid-cols-5 gap-3">
            <MiniStat label="Gold Card" value={abTotals.gold_card || 0} color="bg-yellow-100 text-yellow-800 border-yellow-300" />
            <MiniStat label="Algorithm" value={abTotals.algorithm || 0} color="bg-green-100 text-green-800 border-green-300" />
            <MiniStat label="Manual" value={abTotals.manual || 0} color="bg-blue-100 text-blue-800 border-blue-300" />
            <MiniStat label="Pended" value={abTotals.pended || 0} color="bg-amber-100 text-amber-800 border-amber-300" />
            <MiniStat label="Denied" value={abTotals.denied || 0} color="bg-red-100 text-red-800 border-red-300" />
          </div>

          {/* By CPT Stacked Bar */}
          <div className="border rounded-lg p-4">
            <h2 className="font-semibold mb-1">Approval Type by CPT</h2>
            <p className="text-xs text-gray-400 mb-3">
              How cases get approved — Gold Card bypass vs algorithm auto-approval vs manual
            </p>

            {approvalBreakdown?.by_cpt?.length > 0 ? (
              <div className="space-y-2">
                {approvalBreakdown.by_cpt.map((row: any) => {
                  const total = row.total || 1;
                  return (
                    <div key={row.cpt_code} className="flex items-center gap-2">
                      <span className="text-xs font-mono w-16 text-right">{row.cpt_code}</span>
                      <div className="flex-1 flex h-5 rounded overflow-hidden">
                        {row.gold_card > 0 && <div className="bg-yellow-400" style={{ width: `${(row.gold_card / total) * 100}%` }} title={`Gold Card: ${row.gold_card}`} />}
                        {row.algorithm > 0 && <div className="bg-green-400" style={{ width: `${(row.algorithm / total) * 100}%` }} title={`Algorithm: ${row.algorithm}`} />}
                        {row.manual > 0 && <div className="bg-blue-400" style={{ width: `${(row.manual / total) * 100}%` }} title={`Manual: ${row.manual}`} />}
                        {row.pended > 0 && <div className="bg-amber-400" style={{ width: `${(row.pended / total) * 100}%` }} title={`Pended: ${row.pended}`} />}
                        {row.denied > 0 && <div className="bg-red-400" style={{ width: `${(row.denied / total) * 100}%` }} title={`Denied: ${row.denied}`} />}
                      </div>
                      <span className="text-xs text-gray-400 w-8 text-right">{row.total}</span>
                    </div>
                  );
                })}
                <div className="flex gap-3 text-xs text-gray-500 mt-2 justify-center">
                  <span className="flex items-center gap-1"><span className="w-3 h-3 rounded bg-yellow-400" /> Gold Card</span>
                  <span className="flex items-center gap-1"><span className="w-3 h-3 rounded bg-green-400" /> Algorithm</span>
                  <span className="flex items-center gap-1"><span className="w-3 h-3 rounded bg-blue-400" /> Manual</span>
                  <span className="flex items-center gap-1"><span className="w-3 h-3 rounded bg-amber-400" /> Pended</span>
                  <span className="flex items-center gap-1"><span className="w-3 h-3 rounded bg-red-400" /> Denied</span>
                </div>
              </div>
            ) : (
              <p className="text-sm text-gray-400">No approval data yet. Complete submissions to populate.</p>
            )}
          </div>
        </div>
      )}

      {/* ═══ Pathway Intelligence Tab ═══ */}
      {tab === "pathways" && !tabLoading && (
        <div className="space-y-4">
          <div className="border rounded-lg p-4 border-indigo-200 bg-indigo-50/30">
            <div className="flex items-center gap-2 mb-1">
              <h2 className="font-semibold">Pathway Intelligence</h2>
              <span className="px-2 py-0.5 rounded text-xs font-medium bg-indigo-100 text-indigo-700">Key Insight</span>
            </div>
            <p className="text-xs text-gray-500 mb-3">
              Right pathway + right ICD = approval. Wrong pathway = pend. This is the #1 factor.
            </p>

            {pathwayIntel?.pathways?.length > 0 ? (
              <>
                <table className="w-full text-sm mb-4">
                  <thead>
                    <tr className="text-left text-xs text-gray-500 border-b">
                      <th className="pb-2 font-medium">Pathway</th>
                      <th className="pb-2 font-medium">ICD Codes</th>
                      <th className="pb-2 font-medium text-right">Cases</th>
                      <th className="pb-2 font-medium text-right">Approved</th>
                      <th className="pb-2 font-medium text-right">Pended</th>
                      <th className="pb-2 font-medium text-right">Rate</th>
                      <th className="pb-2 font-medium text-center">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pathwayIntel.pathways.map((p: any) => (
                      <tr key={p.pathway_id} className="border-b">
                        <td className="py-2 text-xs font-medium max-w-[200px] truncate" title={p.pathway_name}>
                          {p.pathway_name || "Unknown"}
                        </td>
                        <td className="py-2 text-xs text-gray-500 max-w-[150px] truncate">
                          {(p.icd_codes || []).join(", ")}
                        </td>
                        <td className="py-2 text-right font-mono text-xs">{p.total_cases}</td>
                        <td className="py-2 text-right font-mono text-xs text-green-600">{p.approved}</td>
                        <td className="py-2 text-right font-mono text-xs text-amber-600">{p.pended + (p.denied || 0)}</td>
                        <td className="py-2 text-right font-mono text-xs">
                          <span className={
                            p.approval_rate >= 0.8 ? "text-green-700 font-bold" :
                            p.approval_rate >= 0.5 ? "text-amber-700" :
                            "text-red-700 font-bold"
                          }>
                            {(p.approval_rate * 100).toFixed(0)}%
                          </span>
                        </td>
                        <td className="py-2 text-center">
                          {p.is_recommended ? (
                            <span className="px-2 py-0.5 rounded text-xs font-medium bg-green-100 text-green-700">Right Path</span>
                          ) : p.approval_rate < 0.5 ? (
                            <span className="px-2 py-0.5 rounded text-xs font-medium bg-red-100 text-red-700">Wrong Path</span>
                          ) : (
                            <span className="text-xs text-gray-400">-</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>

                {/* ICD → Pathway Matrix */}
                {pathwayIntel?.icd_pathway_matrix?.length > 0 && (
                  <div className="border-t pt-3">
                    <h3 className="text-xs font-medium text-gray-500 mb-2">ICD → Pathway Outcome History</h3>
                    <div className="space-y-2">
                      {pathwayIntel.icd_pathway_matrix.map((item: any) => (
                        <div key={item.icd_code} className="flex items-start gap-2 text-xs">
                          <span className="font-mono font-medium w-20 shrink-0">{item.icd_code}</span>
                          <div className="flex flex-wrap gap-1">
                            {item.pathways_tried.map((pt: any, i: number) => {
                              const isRecommended = pt.pathway_id === item.recommended_pathway;
                              const hasApproval = pt.outcomes?.APPROVED > 0;
                              const hasPend = pt.outcomes?.PENDED > 0 || pt.outcomes?.DENIED > 0;
                              return (
                                <span
                                  key={i}
                                  className={`px-2 py-0.5 rounded border text-xs ${
                                    isRecommended
                                      ? "bg-green-50 border-green-300 text-green-800"
                                      : hasPend && !hasApproval
                                      ? "bg-red-50 border-red-300 text-red-700"
                                      : "bg-gray-50 border-gray-200 text-gray-600"
                                  }`}
                                  title={JSON.stringify(pt.outcomes)}
                                >
                                  {pt.pathway_name || "Unknown"}
                                  {isRecommended && " ✓"}
                                </span>
                              );
                            })}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </>
            ) : (
              <p className="text-sm text-gray-400">No pathway data yet. Cases need pathway_id saved to populate this view.</p>
            )}
          </div>
        </div>
      )}

      {/* ═══ Outcome Signal Tab ═══ */}
      {tab === "outcomes" && !tabLoading && (
        <div className="border rounded-lg p-4">
          <h2 className="font-semibold mb-1">Outcome Signal</h2>
          <p className="text-xs text-gray-400 mb-3">
            Approval rates by CPT — ground truth labels
          </p>

          {(!outcomes?.by_cpt || outcomes.by_cpt.length === 0) ? (
            <p className="text-sm text-gray-400">No outcome data yet. Complete submissions to build the dataset.</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500 border-b">
                  <th className="pb-2 font-medium">CPT</th>
                  <th className="pb-2 font-medium text-right">Approved</th>
                  <th className="pb-2 font-medium text-right">Denied</th>
                  <th className="pb-2 font-medium text-right">Pended</th>
                  <th className="pb-2 font-medium text-right">Rate</th>
                </tr>
              </thead>
              <tbody>
                {outcomes.by_cpt.map((r: any) => (
                  <tr key={r.cpt_code} className="border-b">
                    <td className="py-1.5 font-mono text-xs">{r.cpt_code}</td>
                    <td className="py-1.5 text-right font-mono text-green-600">{r.approved}</td>
                    <td className="py-1.5 text-right font-mono text-red-600">{r.denied}</td>
                    <td className="py-1.5 text-right font-mono text-amber-600">{r.pended}</td>
                    <td className="py-1.5 text-right font-mono">{(r.rate * 100).toFixed(1)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {/* Top denial reasons */}
          {outcomes?.top_denial_reasons?.length > 0 && (
            <div className="mt-4 border-t pt-3">
              <h3 className="text-xs font-medium text-gray-500 mb-2">Top Denial Reasons</h3>
              {outcomes.top_denial_reasons.map((d: any, i: number) => (
                <div key={i} className="text-xs text-gray-600 mb-1 flex justify-between">
                  <span className="truncate mr-2">{d.reason}</span>
                  <span className="font-mono text-red-500">{d.count}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ═══ Answer Quality Tab ═══ */}
      {tab === "quality" && !tabLoading && (
        <div className="space-y-4">
          <div className="border rounded-lg p-4">
            <h2 className="font-semibold mb-1">Answer Quality</h2>
            <p className="text-xs text-gray-400 mb-3">
              Rep overrides by CPT — where AI needs improvement
            </p>

            {(!overrides?.by_cpt || overrides.by_cpt.length === 0) ? (
              <p className="text-sm text-gray-400">No override data yet. Process cases to build the dataset.</p>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-gray-500 border-b">
                    <th className="pb-2 font-medium">CPT</th>
                    <th className="pb-2 font-medium text-right">Total</th>
                    <th className="pb-2 font-medium text-right">Overrides</th>
                    <th className="pb-2 font-medium text-right">Rate</th>
                  </tr>
                </thead>
                <tbody>
                  {overrides.by_cpt.map((r: any) => (
                    <tr key={r.cpt_code} className="border-b">
                      <td className="py-1.5 font-mono text-xs">{r.cpt_code}</td>
                      <td className="py-1.5 text-right font-mono">{r.total}</td>
                      <td className="py-1.5 text-right font-mono text-amber-600">{r.overrides}</td>
                      <td className="py-1.5 text-right font-mono">{(r.rate * 100).toFixed(1)}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            {/* Top overridden questions */}
            {overrides?.top_overridden?.length > 0 && (
              <div className="mt-4 border-t pt-3">
                <h3 className="text-xs font-medium text-gray-500 mb-2">Most Overridden Questions</h3>
                {overrides.top_overridden.map((q: any, i: number) => (
                  <div key={i} className="text-xs text-gray-600 mb-1 flex justify-between">
                    <span className="truncate mr-2">{q.question_text}</span>
                    <span className="font-mono text-amber-600 whitespace-nowrap">{q.override_count}/{q.total}</span>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Recent Overrides */}
          {overrides?.recent?.length > 0 && (
            <div className="border rounded-lg p-4">
              <h2 className="font-semibold mb-3">Recent Rep Overrides</h2>
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-gray-500 border-b">
                    <th className="pb-2 font-medium">CPT</th>
                    <th className="pb-2 font-medium">Question</th>
                    <th className="pb-2 font-medium">Rep Answer</th>
                    <th className="pb-2 font-medium">Outcome</th>
                    <th className="pb-2 font-medium">When</th>
                  </tr>
                </thead>
                <tbody>
                  {overrides.recent.map((r: any, i: number) => (
                    <tr key={i} className="border-b">
                      <td className="py-1.5 font-mono text-xs">{r.cpt_code}</td>
                      <td className="py-1.5 text-xs text-gray-600 truncate max-w-[200px]">
                        <Link href={`/cases/${r.case_id}`} className="text-blue-600 hover:underline">
                          {r.question_text}
                        </Link>
                      </td>
                      <td className="py-1.5 text-xs">{r.answer_text || "-"}</td>
                      <td className="py-1.5"><OutcomeBadge outcome={r.outcome} /></td>
                      <td className="py-1.5 text-xs text-gray-400">
                        {r.created_at ? new Date(r.created_at).toLocaleDateString() : "-"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* ═══ Submission Signatures Tab ═══ */}
      {tab === "signatures" && !tabLoading && (
        <div className="border rounded-lg p-4">
          <h2 className="font-semibold mb-1">Submission Signatures</h2>
          <p className="text-xs text-gray-400 mb-3">
            CPT + ICD + Pathway combinations — learnable outcome patterns
          </p>

          {signatures?.signatures?.length > 0 ? (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500 border-b">
                  <th className="pb-2 font-medium">CPT</th>
                  <th className="pb-2 font-medium">ICD</th>
                  <th className="pb-2 font-medium">Pathway</th>
                  <th className="pb-2 font-medium text-right">Total</th>
                  <th className="pb-2 font-medium text-right">Approved</th>
                  <th className="pb-2 font-medium text-right">Pended</th>
                  <th className="pb-2 font-medium text-right">Success</th>
                  <th className="pb-2 font-medium text-right">Last</th>
                </tr>
              </thead>
              <tbody>
                {signatures.signatures.map((s: any, i: number) => (
                  <tr key={i} className="border-b">
                    <td className="py-1.5 font-mono text-xs">{s.cpt_code}</td>
                    <td className="py-1.5 font-mono text-xs text-gray-500">{s.icd_code || "-"}</td>
                    <td className="py-1.5 text-xs truncate max-w-[180px]" title={s.pathway_name}>
                      {s.pathway_name || "-"}
                    </td>
                    <td className="py-1.5 text-right font-mono text-xs">{s.total}</td>
                    <td className="py-1.5 text-right font-mono text-xs text-green-600">{s.approved}</td>
                    <td className="py-1.5 text-right font-mono text-xs text-amber-600">{s.pended + (s.denied || 0)}</td>
                    <td className="py-1.5 text-right">
                      <span className={`font-mono text-xs font-bold ${
                        s.approval_rate >= 80 ? "text-green-700" :
                        s.approval_rate >= 50 ? "text-amber-700" :
                        "text-red-700"
                      }`}>
                        {s.approval_rate}%
                      </span>
                    </td>
                    <td className="py-1.5 text-right text-xs text-gray-400">
                      {s.last_submitted ? new Date(s.last_submitted).toLocaleDateString() : "-"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="text-sm text-gray-400">No submission signatures yet. Complete submissions with pathway data to populate.</p>
          )}
        </div>
      )}

      {/* ═══ Coverage Tab ═══ */}
      {tab === "coverage" && !tabLoading && (
        <div className="border rounded-lg p-4">
          <h2 className="font-semibold mb-1">Pattern Coverage</h2>
          <p className="text-xs text-gray-400 mb-3">
            CPT x ICD coverage depth — deeper = better RAG retrieval
          </p>

          {(!coverage?.cpt_icd_coverage || coverage.cpt_icd_coverage.length === 0) ? (
            <p className="text-sm text-gray-400">No pattern data yet. The dataset grows with every completed submission.</p>
          ) : (
            <div className="grid grid-cols-5 gap-2">
              {coverage.cpt_icd_coverage.map((c: any, i: number) => {
                const depth =
                  c.outcome_count >= 100 ? "bg-green-100 border-green-300" :
                  c.outcome_count >= 50 ? "bg-blue-100 border-blue-300" :
                  c.outcome_count >= 10 ? "bg-amber-100 border-amber-300" :
                  "bg-gray-50 border-gray-200";
                return (
                  <div key={i} className={`border rounded p-2 text-center ${depth}`}>
                    <p className="font-mono text-xs font-medium">{c.cpt_code}</p>
                    <p className="text-xs text-gray-500 truncate">{c.icd1 || "-"}</p>
                    <p className="font-mono text-lg font-bold">{c.outcome_count}</p>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function StatCard({ label, value, sub, color }: { label: string; value: number; sub: string; color: string }) {
  const colors: Record<string, string> = {
    blue: "border-blue-200 bg-blue-50",
    green: "border-green-200 bg-green-50",
    amber: "border-amber-200 bg-amber-50",
    purple: "border-purple-200 bg-purple-50",
    gray: "border-gray-200 bg-gray-50",
  };
  const textColors: Record<string, string> = {
    blue: "text-blue-700",
    green: "text-green-700",
    amber: "text-amber-700",
    purple: "text-purple-700",
    gray: "text-gray-700",
  };
  return (
    <div className={`border rounded-lg p-4 ${colors[color] || colors.gray}`}>
      <p className="text-xs text-gray-500 mb-1">{label}</p>
      <p className={`text-2xl font-bold ${textColors[color] || textColors.gray}`}>{value.toLocaleString()}</p>
      <p className="text-xs text-gray-400 mt-1">{sub}</p>
    </div>
  );
}

function MiniStat({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div className={`border rounded-lg p-3 text-center ${color}`}>
      <p className="text-lg font-bold">{value}</p>
      <p className="text-xs font-medium">{label}</p>
    </div>
  );
}

function OutcomeBadge({ outcome }: { outcome: string | null }) {
  if (!outcome) return <span className="text-xs text-gray-400">-</span>;
  const colors: Record<string, string> = {
    APPROVED: "bg-green-100 text-green-700",
    DENIED: "bg-red-100 text-red-700",
    PENDED: "bg-amber-100 text-amber-700",
  };
  return (
    <span className={`px-2 py-0.5 rounded text-xs font-medium ${colors[outcome] || "bg-gray-100 text-gray-700"}`}>
      {outcome}
    </span>
  );
}
