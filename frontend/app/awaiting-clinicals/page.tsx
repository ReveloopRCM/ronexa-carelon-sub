"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { listQueue } from "@/lib/api";

type TabLevel = "l1" | "l2" | "awaiting" | "clinical_review";

export default function AwaitingClinicalsPage() {
  const [tab, setTab] = useState<TabLevel>("l1");
  const [queue, setQueue] = useState<any[]>([]);
  const [l1Count, setL1Count] = useState(0);
  const [l2Count, setL2Count] = useState(0);
  const [awaitingCount, setAwaitingCount] = useState(0);
  const [clinicalReviewCount, setClinicalReviewCount] = useState(0);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    loadQueue();
    const interval = setInterval(loadQueue, 5000);
    return () => clearInterval(interval);
  }, [tab]);

  async function loadQueue() {
    try {
      // Fetch all tabs in parallel for counts
      const [l1, l2, awaiting, clinicalReview] = await Promise.all([
        listQueue(50, 1, "order"),
        listQueue(50, 2, "order"),
        listQueue(50, "awaiting_clinicals"),
        listQueue(50, "clinical"),
      ]);
      setL1Count(l1.length);
      setL2Count(l2.length);
      setAwaitingCount(awaiting.length);
      setClinicalReviewCount(clinicalReview.length);

      // Set current tab's queue
      if (tab === "l1") setQueue(l1);
      else if (tab === "l2") setQueue(l2);
      else if (tab === "awaiting") setQueue(awaiting);
      else setQueue(clinicalReview);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }

  if (loading) return <p className="text-gray-500">Loading...</p>;

  const tabDescription =
    tab === "l1"
      ? "Order-only cases in first review. Verify AI answers against the physician order form, then send to L2."
      : tab === "l2"
      ? "Order-only cases in final review. Confirm answers and submit to Carelon portal."
      : tab === "clinical_review"
      ? "Signature-replayed cases where portal algorithm approved or Gold Card detected. Verify with clinicals and confirm or reject."
      : "Low-confidence order cases parked for clinical notes. Upload clinicals to re-run with full context.";

  const emptyMessage =
    tab === "l1"
      ? "No order cases in L1 review."
      : tab === "l2"
      ? "No order cases in L2 review."
      : tab === "clinical_review"
      ? "No cases in clinical review."
      : "No cases awaiting clinical upload.";

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Awaiting Clinicals</h1>
        <span className="text-xs text-gray-400">Auto-refreshes every 5s</span>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 border-b">
        <button
          onClick={() => setTab("l1")}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition ${
            tab === "l1"
              ? "border-teal-600 text-teal-600"
              : "border-transparent text-gray-500 hover:text-gray-700"
          }`}
        >
          L1 Review
          {l1Count > 0 && (
            <span className="ml-2 bg-teal-100 text-teal-700 text-xs px-2 py-0.5 rounded-full">
              {l1Count}
            </span>
          )}
        </button>
        <button
          onClick={() => setTab("l2")}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition ${
            tab === "l2"
              ? "border-green-600 text-green-600"
              : "border-transparent text-gray-500 hover:text-gray-700"
          }`}
        >
          L2 Review
          {l2Count > 0 && (
            <span className="ml-2 bg-green-100 text-green-700 text-xs px-2 py-0.5 rounded-full">
              {l2Count}
            </span>
          )}
        </button>
        <button
          onClick={() => setTab("awaiting")}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition ${
            tab === "awaiting"
              ? "border-amber-600 text-amber-600"
              : "border-transparent text-gray-500 hover:text-gray-700"
          }`}
        >
          Awaiting Upload
          {awaitingCount > 0 && (
            <span className="ml-2 bg-amber-100 text-amber-700 text-xs px-2 py-0.5 rounded-full">
              {awaitingCount}
            </span>
          )}
        </button>
        <button
          onClick={() => setTab("clinical_review")}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition ${
            tab === "clinical_review"
              ? "border-violet-600 text-violet-600"
              : "border-transparent text-gray-500 hover:text-gray-700"
          }`}
        >
          Clinical Review
          {clinicalReviewCount > 0 && (
            <span className="ml-2 bg-violet-100 text-violet-700 text-xs px-2 py-0.5 rounded-full">
              {clinicalReviewCount}
            </span>
          )}
        </button>
      </div>

      {/* Tab description */}
      <p className="text-xs text-gray-400">{tabDescription}</p>

      {queue.length === 0 ? (
        <p className="text-gray-500">{emptyMessage}</p>
      ) : (
        <div className="space-y-2">
          {queue.map((c) => (
            <Link
              key={c.id}
              href={
                tab === "awaiting"
                  ? `/cases/${c.id}`
                  : tab === "clinical_review"
                  ? `/queue/${c.id}`
                  : `/queue/${c.id}?level=${tab === "l1" ? 1 : 2}`
              }
              className="block border rounded p-4 hover:border-teal-500 transition"
            >
              <div className="flex items-center justify-between">
                <div>
                  <span className="font-medium">
                    {c.first_name} {c.last_name}
                  </span>
                  <span className="text-sm text-gray-500 ml-3">
                    CPT {c.cpt_code}
                  </span>
                  {c.is_stat && (
                    <span className="ml-2 bg-red-100 text-red-700 text-xs px-2 py-0.5 rounded">
                      STAT
                    </span>
                  )}
                  {tab !== "clinical_review" && (
                    <span className="ml-2 bg-teal-100 text-teal-700 text-xs px-2 py-0.5 rounded">
                      Order Only
                    </span>
                  )}
                  {c.auto_approved === true && (
                    <span className={`ml-2 text-[10px] px-1.5 py-0.5 rounded-full font-medium ${
                      c.approval_type === "gold_card"
                        ? "bg-yellow-100 text-yellow-700"
                        : "bg-green-100 text-green-700"
                    }`}>
                      {c.approval_type === "gold_card" ? "Gold Card" : "Algorithm Approved"}
                    </span>
                  )}
                  {c.auto_approved === false && tab !== "clinical_review" && (
                    <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded-full font-medium bg-amber-100 text-amber-700">
                      Pend
                    </span>
                  )}
                  {tab === "l2" && c.state === "L2_REVIEW" && (
                    <span className="ml-2 bg-amber-100 text-amber-700 text-xs px-2 py-0.5 rounded">
                      L1 reviewed
                    </span>
                  )}
                  {tab === "awaiting" && (
                    <span className="ml-2 bg-amber-100 text-amber-700 text-xs px-2 py-0.5 rounded">
                      Needs Clinicals
                    </span>
                  )}
                  {tab === "clinical_review" && (
                    <span className="ml-2 bg-violet-100 text-violet-700 text-xs px-2 py-0.5 rounded">
                      Signature Replay
                    </span>
                  )}
                  {c.rerun_count > 0 && (
                    <span className="ml-2 bg-amber-100 text-amber-700 text-xs px-2 py-0.5 rounded">
                      Re-run &times;{c.rerun_count}
                    </span>
                  )}
                </div>
                <div className="text-sm text-gray-500">
                  {c.center_abbr} &middot; {c.exam_id}
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
