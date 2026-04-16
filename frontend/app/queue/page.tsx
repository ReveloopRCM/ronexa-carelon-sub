"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { listQueue } from "@/lib/api";

type TabLevel = 1 | 2 | "fax";

export default function QueuePage() {
  const [level, setLevel] = useState<TabLevel>(1);
  const [queue, setQueue] = useState<any[]>([]);
  const [l1Count, setL1Count] = useState(0);
  const [l2Count, setL2Count] = useState(0);
  const [faxCount, setFaxCount] = useState(0);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    loadQueue();
    const interval = setInterval(loadQueue, 5000);
    return () => clearInterval(interval);
  }, [level]);

  async function loadQueue() {
    try {
      // Fetch all tabs in parallel for counts
      const [l1, l2, all] = await Promise.all([
        listQueue(50, 1, "clinical"),
        listQueue(50, 2, "clinical"),
        listQueue(50),  // all states — includes PENDED_FAX_REVIEW
      ]);
      setL1Count(l1.length);
      setL2Count(l2.length);

      // Filter fax review cases from the "all" response
      const faxCases = all.filter((c: any) => c.state === "PENDED_FAX_REVIEW");
      setFaxCount(faxCases.length);

      // Set current tab's queue
      if (level === 1) setQueue(l1);
      else if (level === 2) setQueue(l2);
      else setQueue(faxCases);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }

  if (loading) return <p className="text-gray-500">Loading queue...</p>;

  const tabDescription =
    level === 1
      ? "First review — verify AI answers against clinical notes. Approve or edit, then send to L2."
      : level === 2
      ? "Final review — confirm answers and submit to Carelon portal."
      : "Fax validation — verify fax details before clinical notes are sent to Carelon.";

  const emptyMessage =
    level === 1
      ? "No cases in L1 review."
      : level === 2
      ? "No cases in L2 review."
      : "No cases awaiting fax validation.";

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Review Queue</h1>
        <span className="text-xs text-gray-400">Auto-refreshes every 5s</span>
      </div>

      {/* L1/L2/Fax tabs */}
      <div className="flex gap-1 border-b">
        <button
          onClick={() => setLevel(1)}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition ${
            level === 1
              ? "border-blue-600 text-blue-600"
              : "border-transparent text-gray-500 hover:text-gray-700"
          }`}
        >
          L1 Review
          {l1Count > 0 && (
            <span className="ml-2 bg-blue-100 text-blue-700 text-xs px-2 py-0.5 rounded-full">
              {l1Count}
            </span>
          )}
        </button>
        <button
          onClick={() => setLevel(2)}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition ${
            level === 2
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
          onClick={() => setLevel("fax")}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition ${
            level === "fax"
              ? "border-purple-600 text-purple-600"
              : "border-transparent text-gray-500 hover:text-gray-700"
          }`}
        >
          Fax Review
          {faxCount > 0 && (
            <span className="ml-2 bg-purple-100 text-purple-700 text-xs px-2 py-0.5 rounded-full">
              {faxCount}
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
              href={`/queue/${c.id}?level=${level}`}
              className="block border rounded p-4 hover:border-blue-500 transition"
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
                  {c.approval_type === "no_auth" && (
                    <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded-full font-medium bg-sky-100 text-sky-700">
                      No Auth Required
                    </span>
                  )}
                  {c.gold_card_level != null && (
                    <span className={`ml-2 text-[10px] px-1.5 py-0.5 rounded-full font-medium ${
                      c.gold_card_level >= 2 ? "bg-yellow-100 text-yellow-700" :
                      c.gold_card_level === 1 ? "bg-blue-100 text-blue-700" :
                      "bg-gray-100 text-gray-500"
                    }`}>
                      GC:{c.gold_card_level}
                    </span>
                  )}
                  {c.auto_approved === true && (
                    <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded-full font-medium bg-purple-100 text-purple-700">
                      Auto Approve
                    </span>
                  )}
                  {c.auto_approved === false && !c.approval_type && (
                    <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded-full font-medium bg-amber-100 text-amber-700">
                      Pend
                    </span>
                  )}
                  {c.state === "L2_REVIEW" && level === 2 && (
                    <span className="ml-2 bg-amber-100 text-amber-700 text-xs px-2 py-0.5 rounded">
                      L1 reviewed
                    </span>
                  )}
                  {c.state === "PENDED_FAX_REVIEW" && (
                    <span className="ml-2 bg-purple-100 text-purple-700 text-xs px-2 py-0.5 rounded">
                      Pended — fax pending
                    </span>
                  )}
                  {c.state === "CLINICAL_REVIEW" && (
                    <span className="ml-2 bg-orange-100 text-orange-700 text-xs px-2 py-0.5 rounded">
                      Clinical Review
                    </span>
                  )}
                  {c.state === "WAITING_CLINICALS" && (
                    <span className="ml-2 bg-teal-100 text-teal-700 text-xs px-2 py-0.5 rounded">
                      Order Only — Awaiting Clinicals
                    </span>
                  )}
                  {c.signature_replay && (
                    <span className="ml-2 bg-indigo-100 text-indigo-700 text-xs px-2 py-0.5 rounded">
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
