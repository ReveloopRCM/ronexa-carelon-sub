"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { confirmClinicalReview, confirmNoAuth, flagCase, getQueueItem, markAlreadyWorked, rejectClinicalReview, rejectNoAuth, resolveL1, resolveL2, rerunL2, submitBatchReview, updatePathway, validateFax } from "@/lib/api";

export default function ReviewPage() {
  const params = useParams();
  const router = useRouter();
  const searchParams = useSearchParams();
  const caseId = params.caseId as string;
  const levelParam = searchParams.get("level") || "1";
  const level = levelParam === "clinical" ? "clinical" : (parseInt(levelParam, 10) as 1 | 2);

  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);

  // Track rep edits: { group_id: answer_value }
  const [edits, setEdits] = useState<Record<number, string | string[]>>({});
  const [editNote, setEditNote] = useState("");
  const [faxRejectReason, setFaxRejectReason] = useState("");

  // Toast notification (replaces alert())
  const [toast, setToast] = useState<{ message: string; type: "success" | "error" } | null>(null);

  // Re-run confirmation modal
  const [showRerunModal, setShowRerunModal] = useState(false);
  const [rerunReason, setRerunReason] = useState("");

  // Pathway (clinical scenario) selection
  const [selectedPathway, setSelectedPathway] = useState<string>("");
  const [pathwayChanged, setPathwayChanged] = useState(false);

  // Auto-dismiss toast after 4 seconds
  useEffect(() => {
    if (toast) {
      const timer = setTimeout(() => setToast(null), 4000);
      return () => clearTimeout(timer);
    }
  }, [toast]);

  useEffect(() => {
    loadData();
  }, [caseId]);

  // Initialize pathway selection from case data
  useEffect(() => {
    if (data?.case?.pathway_id) {
      setSelectedPathway(data.case.pathway_id);
      setPathwayChanged(false);
    }
  }, [data]);

  async function loadData() {
    try {
      const d = await getQueueItem(caseId);
      setData(d);
      setEdits({});
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }

  function setAnswer(groupId: number, value: string | string[]) {
    setEdits((prev) => ({ ...prev, [groupId]: value }));
  }

  function getAiValue(q: any): string | string[] {
    // Portal answer format: { Values: ["uuid"] } or { Value: "uuid" }
    if (q.ai_answer?.Values?.length) {
      return q.ai_answer.Values.length === 1
        ? q.ai_answer.Values[0]
        : q.ai_answer.Values;
    }
    if (q.ai_answer?.Value) return q.ai_answer.Value;
    if (Array.isArray(q.ai_answer)) return q.ai_answer;
    if (typeof q.ai_answer === "string") return q.ai_answer;
    // Fallback for dict-shaped answers (auto-fills with MeasureUnitValues etc.)
    return "";
  }

  function getAnswer(q: any): string | string[] {
    if (edits[q.group_id] !== undefined) return edits[q.group_id];
    // L2: show L1's answer if it was edited
    if (level === 2 && q.rep_answer?.Value) {
      return q.rep_answer.Value;
    }
    return getAiValue(q);
  }

  function isEdited(q: any): boolean {
    if (edits[q.group_id] === undefined) return false;
    // Compare against what's currently shown (L1's answer at L2, else AI answer)
    let currentVal: string | string[];
    if (level === 2 && q.rep_answer?.Value) {
      currentVal = q.rep_answer.Value;
    } else {
      currentVal = getAiValue(q);
    }
    const editVal = edits[q.group_id];
    if (Array.isArray(currentVal) && Array.isArray(editVal)) {
      return JSON.stringify(currentVal.sort()) !== JSON.stringify(editVal.sort());
    }
    return editVal !== currentVal;
  }

  const hasEdits = data?.questions?.some((q: any) => isEdited(q)) ?? false;

  async function handleSubmit() {
    if (!data?.questions?.length) return;
    setSubmitting(true);
    try {
      const answers = data.questions
        .filter((q: any) => q.review_state === "AI_SUGGESTED" || q.review_state === "REP_APPROVED" || q.review_state === "REP_EDITED")
        .map((q: any) => ({
          group_id: q.group_id,
          answer_value: getAnswer(q),
        }));

      const payload = { rep_id: `rep_${level}`, answers, note: editNote || undefined };

      // Call the right endpoint based on review level
      const result = level === 2
        ? await resolveL2(caseId, payload)
        : await resolveL1(caseId, payload);

      if (result.status === "sent_to_l2") {
        // L1 done — sent to L2 queue
        router.push("/queue");
      } else if (result.edited) {
        // Backtrack triggered — reload to see new questions
        await loadData();
      } else {
        // Submitted or approved — go back to queue
        router.push("/queue");
      }
    } catch (err: any) {
      setToast({ message: err.message || "An error occurred", type: "error" });
    } finally {
      setSubmitting(false);
    }
  }

  async function handleRerunConfirm() {
    if (!data?.questions?.length) return;
    setSubmitting(true);
    try {
      const answers = data.questions
        .filter((q: any) => q.review_state === "AI_SUGGESTED" || q.review_state === "REP_APPROVED" || q.review_state === "REP_EDITED")
        .map((q: any) => ({
          group_id: q.group_id,
          answer_value: getAnswer(q),
        }));

      const payload = { rep_id: `rep_${level}`, answers, note: rerunReason || editNote || undefined };
      await rerunL2(caseId, payload);
      setShowRerunModal(false);
      setToast({ message: "Re-run triggered — case will return to L1 Review with new questions.", type: "success" });
      setTimeout(() => router.push("/queue"), 1200);
    } catch (err: any) {
      setToast({ message: err.message || "Error triggering re-run", type: "error" });
    } finally {
      setSubmitting(false);
    }
  }

  async function handleFlag() {
    setSubmitting(true);
    try {
      await flagCase(caseId, "rep_1", "Missing documentation");
      router.push("/queue");
    } catch (err: any) {
      setToast({ message: err.message || "An error occurred", type: "error" });
    } finally {
      setSubmitting(false);
    }
  }

  async function handleAlreadyWorked() {
    setSubmitting(true);
    try {
      await markAlreadyWorked(caseId, { rep_id: "rep_1", note: "Worked in legacy system" });
      router.push("/queue");
    } catch (err: any) {
      setToast({ message: err.message || "An error occurred", type: "error" });
    } finally {
      setSubmitting(false);
    }
  }

  async function handleFaxValidate(action: "approved" | "rejected") {
    setSubmitting(true);
    try {
      const result = await validateFax(caseId, {
        action,
        rep_id: "fax_rep",
        reason: action === "rejected" ? faxRejectReason || "Rejected by rep" : undefined,
      });
      if (action === "approved") {
        if (result.fax_sent) {
          setToast({ message: `Fax sent successfully (ID: ${result.message_id})`, type: "success" });
        } else {
          setToast({ message: `Fax failed: ${result.fax_error || "Unknown error"}`, type: "error" });
        }
        setTimeout(() => router.push("/queue"), 2000);
      } else {
        setToast({ message: "Fax rejected — case moved to Pended", type: "success" });
        setTimeout(() => router.push("/queue"), 1500);
      }
    } catch (err: any) {
      setToast({ message: err.message || "An error occurred", type: "error" });
    } finally {
      setSubmitting(false);
    }
  }

  async function handleClinicalConfirm() {
    setSubmitting(true);
    try {
      await confirmClinicalReview(caseId, {
        rep_id: "clinical_rep",
        note: editNote || undefined,
      });
      setToast({ message: "Clinical review confirmed — case queued for submission", type: "success" });
      setTimeout(() => router.push("/queue"), 1000);
    } catch (err: any) {
      setToast({ message: err.message || "An error occurred", type: "error" });
    } finally {
      setSubmitting(false);
    }
  }

  async function handleClinicalReject() {
    setSubmitting(true);
    try {
      await rejectClinicalReview(caseId, {
        rep_id: "clinical_rep",
        note: editNote || "Clinicals don't match signature answers",
      });
      setToast({ message: "Clinical review rejected — case reset for re-processing", type: "success" });
      setTimeout(() => router.push("/queue"), 1000);
    } catch (err: any) {
      setToast({ message: err.message || "An error occurred", type: "error" });
    } finally {
      setSubmitting(false);
    }
  }

  async function handleConfirmNoAuth() {
    setSubmitting(true);
    try {
      await confirmNoAuth(caseId, { rep_id: "rep", note: editNote || undefined });
      setToast({ message: "Confirmed — No auth required", type: "success" });
      setTimeout(() => router.push("/queue"), 1000);
    } catch (e: any) {
      setToast({ message: e.message || "Failed", type: "error" });
    } finally {
      setSubmitting(false);
    }
  }

  async function handleRejectNoAuth() {
    setSubmitting(true);
    try {
      await rejectNoAuth(caseId, { rep_id: "rep", note: editNote || undefined });
      setToast({ message: "Rejected — Case will be reprocessed", type: "success" });
      setTimeout(() => router.push("/queue"), 1000);
    } catch (e: any) {
      setToast({ message: e.message || "Failed", type: "error" });
    } finally {
      setSubmitting(false);
    }
  }

  async function handlePathwayChange(id: string, name: string) {
    setSelectedPathway(id);
    if (id !== data?.case?.pathway_id) {
      setPathwayChanged(true);
      try {
        await updatePathway(caseId, { pathway_id: id, pathway_name: name, rep_id: `rep_${level}` });
        // Auto-trigger rerun — changing the clinical scenario invalidates all downstream questions
        setRerunReason(`Clinical scenario changed to "${name}" — all downstream questions need re-evaluation.`);
        setShowRerunModal(true);
      } catch (err: any) {
        setToast({ message: err.message || "Error changing pathway", type: "error" });
        setSelectedPathway(data?.case?.pathway_id || "");
        setPathwayChanged(false);
      }
    } else {
      setPathwayChanged(false);
    }
  }

  if (loading) return <p className="text-gray-500">Loading...</p>;
  if (!data) return <p className="text-red-500">Case not found</p>;

  const { case: caseInfo, questions, clinical_notes } = data;
  const isFaxReview = caseInfo.state === "PENDED_FAX_REVIEW";
  const isClinicalReview = caseInfo.state === "CLINICAL_REVIEW";
  // Both L1 and L2 see all questions — same view, two pairs of eyes
  const pendingQuestions = questions || [];
  const isNoAuthReview = caseInfo.state === "L1_REVIEW"
    && pendingQuestions.length === 0
    && (caseInfo.hold_reason?.includes("does not require") || caseInfo.hold_reason?.includes("No auth required"));

  return (
    <>
    {/* Toast notification */}
    {toast && (
      <div className={`fixed top-4 right-4 z-50 px-4 py-3 rounded-lg shadow-lg text-sm text-white transition-opacity ${
        toast.type === "success" ? "bg-green-600" : "bg-red-600"
      }`}>
        {toast.message}
      </div>
    )}

    {/* Re-run confirmation modal */}
    {showRerunModal && (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
        <div className="bg-white rounded-lg shadow-xl p-6 max-w-md w-full mx-4">
          <h3 className="font-semibold text-lg mb-2">Confirm Re-Run</h3>
          <p className="text-sm text-gray-600 mb-4">
            {pathwayChanged
              ? "Changing the clinical scenario invalidates all downstream questions. A Re-Run will re-evaluate the entire question tree with the new scenario."
              : "Re-Run will reset this case to First Pass and require L1 re-review."}
          </p>
          <label className="text-xs font-medium text-gray-700 block mb-1">
            Reason for re-run <span className="text-gray-400">(visible to L1)</span>
          </label>
          <textarea
            value={rerunReason}
            onChange={(e) => setRerunReason(e.target.value)}
            placeholder="What should L1 focus on in the next pass..."
            className="w-full border rounded px-3 py-2 text-sm resize-none mb-4"
            rows={3}
            autoFocus
          />
          <div className="flex gap-2 justify-end">
            <button
              onClick={() => {
                setShowRerunModal(false);
                // If pathway was changed but rerun was cancelled, revert the pathway visually
                // (the API already saved it, but at least show user it needs rerun)
                if (pathwayChanged) {
                  setToast({ message: "Pathway was saved — click Re-Run to apply changes", type: "success" });
                }
              }}
              className="px-4 py-2 rounded text-sm text-gray-600 bg-gray-100 hover:bg-gray-200"
            >
              Cancel
            </button>
            <button
              onClick={handleRerunConfirm}
              disabled={submitting}
              className="px-4 py-2 rounded text-sm text-white bg-amber-600 hover:bg-amber-700 disabled:opacity-50"
            >
              {submitting ? "Re-Running..." : "Confirm Re-Run"}
            </button>
          </div>
        </div>
      </div>
    )}

    <div className="grid grid-cols-12 gap-6 h-[calc(100vh-140px)]">
      {/* LEFT — Case Summary + Clinical Notes */}
      <div className="col-span-4 space-y-4 overflow-y-auto pr-2">
        {/* Case Summary */}
        <div className="border rounded p-4 space-y-3">
          <h2 className="font-semibold">Case Summary</h2>

          {/* Portal Verdict Banner — shows algorithm recommendation from clinical decision tree */}
          {caseInfo.auto_approved !== undefined && caseInfo.auto_approved !== null && (
            <div className={`rounded-md px-3 py-2 text-sm font-medium flex items-center gap-2 ${
              caseInfo.auto_approved
                ? "bg-green-50 border border-green-300 text-green-800"
                : "bg-amber-50 border border-amber-300 text-amber-800"
            }`}>
              <span className="text-base">
                {caseInfo.auto_approved ? "✓" : "⚠"}
              </span>
              <span>
                {caseInfo.auto_approved
                  ? "Algorithm Approved — Clinical Criteria Met"
                  : "Algorithm Pend — Review Required"}
                {caseInfo.gold_card_level != null && (
                  <span className={`ml-2 text-xs px-1.5 py-0.5 rounded-full ${
                    caseInfo.gold_card_level >= 2 ? "bg-yellow-100 text-yellow-700" :
                    caseInfo.gold_card_level === 1 ? "bg-blue-100 text-blue-700" :
                    "bg-gray-100 text-gray-500"
                  }`}>
                    GC:{caseInfo.gold_card_level}
                  </span>
                )}
              </span>
            </div>
          )}

          <div className="text-sm space-y-1">
            <p>
              <span className="text-gray-500">Patient:</span>{" "}
              {caseInfo.first_name} {caseInfo.last_name}
            </p>
            <p>
              <span className="text-gray-500">DOB:</span> {caseInfo.dob}
            </p>
            <p>
              <span className="text-gray-500">Policy:</span>{" "}
              {caseInfo.policy_num}
            </p>
            <p>
              <span className="text-gray-500">CPT:</span> {caseInfo.cpt_code}
            </p>
            <p>
              <span className="text-gray-500">ICD:</span>{" "}
              {caseInfo.icd1 || "\u2014"}
            </p>
            <p>
              <span className="text-gray-500">Center:</span>{" "}
              {caseInfo.center_abbr}
            </p>
          </div>
          {/* PDF Link */}
          {(caseInfo.clinical_blob_key || caseInfo.file_key) && (
            <a
              href={`/api/cases/${caseId}/clinical-pdf`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-sm text-blue-600 hover:text-blue-800 flex items-center gap-1"
            >
              <svg
                className="w-4 h-4"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"
                />
              </svg>
              {caseInfo.clinical_blob_key ? "View Clinical PDF" : "View Order PDF"}
            </a>
          )}
        </div>

        {/* Clinical Scenario — pathway selection */}
        {caseInfo.pathway_name && (
          <div className="border rounded p-4 space-y-3">
            <div className="flex items-center justify-between">
              <h2 className="font-semibold">Clinical Scenario</h2>
              {pathwayChanged && (
                <span className="text-xs text-amber-600 font-medium">Changed — Re-Run to apply</span>
              )}
            </div>

            <p className="text-xs text-gray-500">
              AI selected: <span className="font-medium">{caseInfo.pathway_name}</span>
            </p>

            {caseInfo.pathway_options?.length > 0 ? (
              <div className="grid grid-cols-1 gap-2">
                {caseInfo.pathway_options.map((opt: any) => {
                  const isSelected = selectedPathway === opt.id;
                  const isAiPick = opt.id === caseInfo.pathway_id;
                  return (
                    <label
                      key={opt.id}
                      className={`flex items-center gap-2 border rounded px-3 py-2 cursor-pointer text-sm transition ${
                        isSelected
                          ? pathwayChanged ? "border-amber-400 bg-amber-50" : "border-indigo-400 bg-indigo-50"
                          : "border-gray-200 hover:border-gray-300"
                      }`}
                    >
                      <input
                        type="radio"
                        name="pathway"
                        value={opt.id}
                        checked={isSelected}
                        onChange={() => handlePathwayChange(opt.id, opt.text)}
                        className="accent-indigo-600"
                      />
                      <span className="flex-1">{opt.text}</span>
                      {isAiPick && (
                        <span className="text-xs bg-blue-100 text-blue-600 px-1.5 py-0.5 rounded flex-shrink-0">AI</span>
                      )}
                    </label>
                  );
                })}
              </div>
            ) : (
              <p className="text-sm text-indigo-700 font-medium">{caseInfo.pathway_name}</p>
            )}
          </div>
        )}

        {/* Clinical Notes (extracted) */}
        <div className="border rounded p-4 space-y-3">
          <h2 className="font-semibold">Extracted Clinical Data</h2>
          {clinical_notes?.length > 0 ? (
            clinical_notes.map((note: any) => (
              <div key={note.id} className="space-y-2">
                {note.structured && (
                  <div className="text-xs space-y-1 bg-gray-50 p-3 rounded">
                    {/* OCR Text from Document Intelligence */}
                    {note.structured.text && (
                      <div className="mb-2">
                        <span className="font-medium text-gray-500 uppercase text-[10px]">
                          OCR Text ({Math.round(note.structured.text.length / 1000)}K chars)
                        </span>
                        <div className="mt-1 max-h-60 overflow-y-auto bg-white rounded p-2 border">
                          <pre className="whitespace-pre-wrap font-mono text-gray-700 leading-relaxed">
                            {note.structured.text.slice(0, 5000)}
                            {note.structured.text.length > 5000 && "\n\n... (truncated)"}
                          </pre>
                        </div>
                      </div>
                    )}
                    {note.structured.clinical_indication && (
                      <div className="bg-blue-50 border border-blue-100 rounded p-2 mb-2">
                        <span className="font-medium text-gray-500 uppercase text-[10px]">
                          Clinical Indication
                        </span>
                        <p className="mt-0.5">
                          {note.structured.clinical_indication}
                        </p>
                      </div>
                    )}
                    {note.structured.chief_complaint && (
                      <p>
                        <span className="font-medium">Chief complaint:</span>{" "}
                        {note.structured.chief_complaint}
                      </p>
                    )}
                    {note.structured.diagnoses?.length > 0 && (
                      <p>
                        <span className="font-medium">Diagnoses:</span>{" "}
                        {note.structured.diagnoses.join("; ")}
                      </p>
                    )}
                    {note.structured.findings?.length > 0 && (
                      <div>
                        <span className="font-medium">Findings:</span>
                        <ul className="list-disc list-inside ml-1">
                          {note.structured.findings
                            .slice(0, 6)
                            .map((f: string, i: number) => (
                              <li key={i}>{f}</li>
                            ))}
                          {note.structured.findings.length > 6 && (
                            <li className="text-gray-400">
                              +{note.structured.findings.length - 6} more
                            </li>
                          )}
                        </ul>
                      </div>
                    )}
                    {note.structured.medications?.length > 0 && (
                      <p>
                        <span className="font-medium">Medications:</span>{" "}
                        {note.structured.medications.join(", ")}
                      </p>
                    )}
                    {note.structured.prior_treatments?.length > 0 && (
                      <p>
                        <span className="font-medium">Prior treatments:</span>{" "}
                        {note.structured.prior_treatments.join("; ")}
                      </p>
                    )}
                    {note.structured.symptom_duration && (
                      <p>
                        <span className="font-medium">Duration:</span>{" "}
                        {note.structured.symptom_duration}
                      </p>
                    )}
                    {note.structured.body_part && (
                      <p>
                        <span className="font-medium">Body part:</span>{" "}
                        {note.structured.body_part}
                        {note.structured.laterality &&
                          ` (${note.structured.laterality})`}
                      </p>
                    )}
                    {note.structured.relevant_history && (
                      <p>
                        <span className="font-medium">History:</span>{" "}
                        {note.structured.relevant_history}
                      </p>
                    )}
                  </div>
                )}
              </div>
            ))
          ) : (
            <p className="text-sm text-gray-500">No clinical notes.</p>
          )}
        </div>
      </div>

      {/* RIGHT — No Auth Review, Clinical Review, Fax Validation, or Questions */}
      {isNoAuthReview ? (
        <div className="col-span-8 border rounded overflow-y-auto">
          <div className="sticky top-0 bg-white border-b px-4 py-3 z-10">
            <div className="flex items-center gap-2 mb-2">
              <h2 className="font-semibold">No Authorization Required</h2>
              <span className="text-xs bg-blue-100 text-blue-700 px-2 py-0.5 rounded">
                Portal Determination
              </span>
            </div>
            <p className="text-xs text-gray-500">
              The portal determined that DI does not require pre-authorization for this member.
            </p>
          </div>

          <div className="p-6 space-y-6">
            {/* Portal Order Summary screenshot or text fallback */}
            {caseInfo.auth_pdf_url ? (
              <div className="space-y-3">
                <div className="border rounded-lg overflow-hidden bg-gray-50">
                  <p className="text-xs text-gray-500 px-3 py-2 border-b bg-white font-medium">
                    Portal Order Summary
                  </p>
                  <img
                    src={`/api/cases/${caseId}/auth-pdf`}
                    alt="Order Summary from portal"
                    className="w-full"
                  />
                </div>
                <p className="text-xs text-gray-500">
                  {caseInfo.hold_reason || "DI does not require pre-authorization"}
                </p>
              </div>
            ) : (
              <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
                <p className="text-sm font-medium text-blue-800 mb-1">Portal Message</p>
                <p className="text-sm text-blue-700">
                  {caseInfo.hold_reason || "DI does not require pre-authorization"}
                </p>
              </div>
            )}

            {/* Case summary */}
            <div className="grid grid-cols-2 gap-4 text-sm">
              <div>
                <span className="text-gray-500">CPT:</span> {caseInfo.cpt_code}
              </div>
              <div>
                <span className="text-gray-500">ICD:</span> {caseInfo.icd1}
              </div>
              <div>
                <span className="text-gray-500">Policy:</span> {caseInfo.policy_num}
              </div>
              <div>
                <span className="text-gray-500">Center:</span> {caseInfo.center_abbr}
              </div>
            </div>

            {/* Note */}
            <textarea
              value={editNote}
              onChange={(e) => setEditNote(e.target.value)}
              placeholder="Optional note..."
              className="w-full border rounded px-3 py-2 text-sm resize-none"
              rows={2}
            />

            {/* Actions */}
            <div className="flex gap-3">
              <button
                onClick={handleConfirmNoAuth}
                disabled={submitting}
                className="px-4 py-2 bg-green-600 text-white rounded hover:bg-green-700 disabled:opacity-50 text-sm"
              >
                Confirm — No Auth Needed
              </button>
              <button
                onClick={handleRejectNoAuth}
                disabled={submitting}
                className="px-4 py-2 bg-amber-500 text-white rounded hover:bg-amber-600 disabled:opacity-50 text-sm"
              >
                Reject — Re-process
              </button>
              <button
                onClick={handleAlreadyWorked}
                disabled={submitting}
                className="px-4 py-2 bg-gray-200 text-gray-700 rounded hover:bg-gray-300 disabled:opacity-50 text-sm"
              >
                Already Worked
              </button>
            </div>
          </div>
        </div>
      ) : isClinicalReview ? (
        <div className="col-span-8 border rounded overflow-y-auto">
          <div className="sticky top-0 bg-white border-b px-4 py-3 z-10">
            <div className="flex items-center gap-2 mb-2">
              <h2 className="font-semibold">Clinical Review</h2>
              <span className="text-xs bg-orange-100 text-orange-700 px-2 py-0.5 rounded">
                Signature Replay — Verify with Clinicals
              </span>
              {caseInfo.auto_approved && (
                <span className="text-xs bg-green-100 text-green-700 px-2 py-0.5 rounded">
                  Algorithm Approved
                </span>
              )}
              {(caseInfo.gold_card_level || 0) >= 2 && (
                <span className="text-xs bg-yellow-100 text-yellow-700 px-2 py-0.5 rounded">
                  Gold Card
                </span>
              )}
            </div>
            <p className="text-xs text-gray-500">
              This case was processed via algorithm signature replay (no clinicals at time of processing).
              Review the answers below against the clinical notes, then confirm or reject.
            </p>
          </div>

          <div className="p-6 space-y-6">
            {/* Signature Info */}
            <div className="bg-orange-50 border border-orange-200 rounded-lg p-5 space-y-3">
              <h3 className="font-semibold text-orange-800">Signature Replay Details</h3>
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div>
                  <span className="text-gray-500">CPT Code:</span>
                  <p className="font-medium">{caseInfo.cpt_code}</p>
                </div>
                <div>
                  <span className="text-gray-500">ICD-10:</span>
                  <p className="font-medium">{caseInfo.icd1 || "—"}</p>
                </div>
                <div>
                  <span className="text-gray-500">Pathway:</span>
                  <p className="font-medium">{caseInfo.pathway_name || "—"}</p>
                </div>
                <div>
                  <span className="text-gray-500">Algorithm Result:</span>
                  <p className="font-medium">
                    {caseInfo.algorithm_recommendation === 3 ? "Approved (RecType=3)" : `RecType=${caseInfo.algorithm_recommendation || "—"}`}
                  </p>
                </div>
              </div>
            </div>

            {/* Questions from signature replay */}
            {pendingQuestions.length > 0 && (
              <div className="space-y-3">
                <h3 className="font-semibold">Replayed Questions ({pendingQuestions.length})</h3>
                {pendingQuestions.map((q: any, idx: number) => (
                  <div key={q.id} className="border rounded-lg p-4 bg-gray-50">
                    <div className="flex items-start gap-2 mb-2">
                      <span className="text-xs font-mono bg-gray-200 px-2 py-0.5 rounded">Q{idx + 1}</span>
                      <p className="text-sm font-medium">{q.question_text}</p>
                    </div>
                    <div className="ml-8 text-sm">
                      <div className="flex items-center gap-2">
                        <span className="text-gray-500">Answer:</span>
                        <span className="font-medium text-blue-700">
                          {q.ai_answer
                            ? (q.options_json || []).find((o: any) => o.id === q.ai_answer)?.text
                              || (Array.isArray(q.ai_answer)
                                ? q.ai_answer.map((a: string) => (q.options_json || []).find((o: any) => o.id === a)?.text || a).join(", ")
                                : String(q.ai_answer))
                            : "—"}
                        </span>
                      </div>
                      {q.ai_evidence && (
                        <p className="text-xs text-gray-500 mt-1">
                          {q.ai_evidence}
                        </p>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {/* Note */}
            <div className="space-y-2">
              <label className="text-sm font-medium text-gray-700">
                Review Note (optional)
              </label>
              <textarea
                value={editNote}
                onChange={(e) => setEditNote(e.target.value)}
                placeholder="Add a note about your review..."
                className="w-full border rounded px-3 py-2 text-sm resize-none"
                rows={2}
              />
            </div>

            {/* Action buttons */}
            <div className="flex gap-3 pt-2">
              <button
                onClick={handleClinicalConfirm}
                disabled={submitting}
                className="flex-1 bg-green-600 text-white px-6 py-3 rounded-lg text-sm font-medium hover:bg-green-700 disabled:opacity-50"
              >
                {submitting ? "Processing..." : "Confirm — Submit to Portal"}
              </button>
              <button
                onClick={handleClinicalReject}
                disabled={submitting}
                className="flex-1 border border-amber-500 text-amber-600 px-6 py-3 rounded-lg text-sm font-medium hover:bg-amber-50 disabled:opacity-50"
              >
                {submitting ? "Processing..." : "Reject — Re-run with Clinicals"}
              </button>
              <button
                onClick={handleAlreadyWorked}
                disabled={submitting}
                className="border border-gray-300 text-gray-500 px-4 py-3 rounded-lg text-sm font-medium hover:bg-gray-100 disabled:opacity-50"
              >
                Already Worked
              </button>
            </div>
          </div>
        </div>
      ) : isFaxReview ? (
        <div className="col-span-8 border rounded overflow-y-auto">
          <div className="sticky top-0 bg-white border-b px-4 py-3 z-10">
            <div className="flex items-center gap-2 mb-2">
              <h2 className="font-semibold">Fax Validation</h2>
              <span className="text-xs bg-purple-100 text-purple-700 px-2 py-0.5 rounded">
                Pended — Verify before faxing
              </span>
            </div>
            <p className="text-xs text-gray-500">
              This case was pended by Carelon. Review the details below and approve to fax clinical notes.
            </p>
          </div>

          <div className="p-6 space-y-6">
            {/* Fax Details */}
            <div className="bg-purple-50 border border-purple-200 rounded-lg p-5 space-y-3">
              <h3 className="font-semibold text-purple-800">Fax Details</h3>
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div>
                  <span className="text-gray-500">Fax Destination:</span>
                  <p className="font-medium">Carelon +1 (800) 798-2068</p>
                </div>
                <div>
                  <span className="text-gray-500">Order ID:</span>
                  <p className="font-medium">{caseInfo.auth_number || caseInfo.portal_case_id || "—"}</p>
                </div>
                <div>
                  <span className="text-gray-500">Determination:</span>
                  <p className="font-medium">{caseInfo.determination_status || "In Progress (Pended)"}</p>
                </div>
                <div>
                  <span className="text-gray-500">Pend Reason:</span>
                  <p className="font-medium">{caseInfo.pend_reason || "Additional documentation requested"}</p>
                </div>
              </div>
            </div>

            {/* Patient Summary */}
            <div className="bg-gray-50 border rounded-lg p-5 space-y-3">
              <h3 className="font-semibold">Patient &amp; Case Info</h3>
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div>
                  <span className="text-gray-500">Patient:</span>
                  <p className="font-medium">{caseInfo.first_name} {caseInfo.last_name}</p>
                </div>
                <div>
                  <span className="text-gray-500">DOB:</span>
                  <p className="font-medium">{caseInfo.dob}</p>
                </div>
                <div>
                  <span className="text-gray-500">Member ID:</span>
                  <p className="font-medium">{caseInfo.policy_num}</p>
                </div>
                <div>
                  <span className="text-gray-500">CPT:</span>
                  <p className="font-medium">{caseInfo.cpt_code}</p>
                </div>
              </div>
            </div>

            {/* Clinical Documents */}
            <div className="bg-blue-50 border border-blue-200 rounded-lg p-5 space-y-3">
              <h3 className="font-semibold text-blue-800">Documents to Fax</h3>
              {(caseInfo.clinical_blob_key || caseInfo.file_key) ? (
                <div className="flex items-center gap-3">
                  <a
                    href={`/api/cases/${caseId}/clinical-pdf`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-sm text-blue-600 hover:text-blue-800 flex items-center gap-1 underline"
                  >
                    {caseInfo.clinical_blob_key ? "View Clinical PDF" : "View Order PDF"}
                  </a>
                  <span className="text-xs text-gray-500">
                    ({caseInfo.clinical_blob_key || caseInfo.file_key})
                  </span>
                </div>
              ) : (
                <p className="text-sm text-red-500">No clinical documents available to fax.</p>
              )}
            </div>

            {/* Reject reason */}
            <div className="space-y-2">
              <label className="text-sm font-medium text-gray-700">
                Rejection Reason (optional)
              </label>
              <textarea
                value={faxRejectReason}
                onChange={(e) => setFaxRejectReason(e.target.value)}
                placeholder="If rejecting, explain why..."
                className="w-full border rounded px-3 py-2 text-sm resize-none"
                rows={2}
              />
            </div>

            {/* Action buttons */}
            <div className="flex gap-3 pt-2">
              <button
                onClick={() => handleFaxValidate("approved")}
                disabled={submitting}
                className="flex-1 bg-green-600 text-white px-6 py-3 rounded-lg text-sm font-medium hover:bg-green-700 disabled:opacity-50"
              >
                {submitting ? "Processing..." : "Approve — Send Fax"}
              </button>
              <button
                onClick={() => handleFaxValidate("rejected")}
                disabled={submitting}
                className="flex-1 border border-red-500 text-red-600 px-6 py-3 rounded-lg text-sm font-medium hover:bg-red-50 disabled:opacity-50"
              >
                {submitting ? "Processing..." : "Reject — Do Not Fax"}
              </button>
            </div>
          </div>
        </div>
      ) : (
      <div className="col-span-8 border rounded overflow-y-auto">
        <div className="sticky top-0 bg-white border-b px-4 py-3 flex items-center justify-between z-10">
          <div>
            <div className="flex items-center gap-2">
              <h2 className="font-semibold">
                Clinical Questions ({pendingQuestions.length})
              </h2>
              <span className={`text-xs px-2 py-0.5 rounded ${
                level === 1 ? "bg-blue-100 text-blue-700" : "bg-green-100 text-green-700"
              }`}>
                {level === 1 ? "L1 Review" : "L2 Final Review"}
              </span>
              {caseInfo.rerun_count > 0 && (
                <span className="text-xs px-2 py-0.5 rounded bg-gray-100 text-gray-600">
                  Pass #{caseInfo.rerun_count + 1}
                </span>
              )}
            </div>
            {level === 2 && (
              <p className="text-xs text-gray-500 mt-0.5">
                Senior review — verify AI answers before portal submission
              </p>
            )}
            {hasEdits && (
              <p className="text-xs text-amber-600 mt-0.5">
                Edits detected — submitting will trigger backtrack from earliest
                change
              </p>
            )}
          </div>
          <div className="flex gap-2">
            <button
              onClick={handleAlreadyWorked}
              disabled={submitting}
              className="border border-gray-300 text-gray-600 px-3 py-1.5 rounded text-sm hover:bg-gray-100 disabled:opacity-50"
            >
              Already Worked
            </button>
            <button
              onClick={handleFlag}
              disabled={submitting}
              className="border border-amber-500 text-amber-600 px-3 py-1.5 rounded text-sm hover:bg-amber-50 disabled:opacity-50"
            >
              Flag
            </button>
            {/* L2 Re-Run button — opens confirmation modal */}
            {level === 2 && (
              <button
                onClick={() => { setRerunReason(""); setShowRerunModal(true); }}
                disabled={submitting}
                className="px-4 py-1.5 rounded text-sm text-white disabled:opacity-50 bg-amber-600 hover:bg-amber-700"
              >
                Re-Run
              </button>
            )}
            <button
              onClick={handleSubmit}
              disabled={submitting || pendingQuestions.length === 0}
              className={`px-4 py-1.5 rounded text-sm text-white disabled:opacity-50 ${
                level === 2
                  ? "bg-green-600 hover:bg-green-700"
                  : "bg-blue-600 hover:bg-blue-700"
              }`}
            >
              {submitting
                ? level === 2 ? "Submitting to Portal..." : "Sending to L2..."
                : level === 2
                  ? "Submit to Portal"
                  : hasEdits ? "Send to L2 with Edits" : "Approve & Send to L2"}
            </button>
          </div>
        </div>

        {/* L2 rerun note — shown to L1 when case was re-run */}
        {level === 1 && caseInfo.rerun_note && (
          <div className="px-4 py-3 bg-amber-50 border-b flex items-start gap-2">
            <span className="text-amber-600 text-sm mt-0.5">&#x27F3;</span>
            <div>
              <p className="text-xs font-semibold text-amber-700">L2 requested re-run</p>
              <p className="text-sm text-amber-800">{caseInfo.rerun_note}</p>
            </div>
          </div>
        )}

        {/* Edit reason — shown when rep changes answers */}
        {hasEdits && (
          <div className="px-4 py-3 bg-amber-50 border-b">
            <label className="text-xs font-medium text-amber-700 block mb-1">
              {level === 1 ? "L1 Reason for Change" : "L2 Reason for Change"} (required when editing)
            </label>
            <textarea
              value={editNote}
              onChange={(e) => setEditNote(e.target.value)}
              placeholder="Explain why you changed the AI's answer..."
              className="w-full border border-amber-300 rounded px-3 py-2 text-sm resize-none"
              rows={2}
            />
          </div>
        )}

        <div className="divide-y">
          {pendingQuestions.length === 0 ? (
            <div className="text-center py-12">
              <p className="text-gray-500">
                No pending questions. Waiting for workflow...
              </p>
            </div>
          ) : (
            pendingQuestions.map((q: any, idx: number) => {
              const currentAnswer = getAnswer(q);
              const edited = isEdited(q);
              // Only show L1 Changed if L1 actually changed the answer (has rep_answer)
              const l1Edited = level === 2 && q.review_state === "REP_EDITED" && q.rep_answer;

              return (
                <div
                  key={q.id}
                  className={`px-4 py-4 space-y-3 ${
                    edited ? "bg-amber-50" : l1Edited && level === 2 ? "bg-blue-50" : ""
                  }`}
                >
                  {/* Question header */}
                  <div className="flex items-start justify-between gap-4">
                    <div className="flex-1">
                      <div className="flex items-center gap-2 mb-1">
                        <span className="text-xs font-mono bg-gray-100 px-1.5 py-0.5 rounded">
                          Q{idx + 1}
                        </span>
                        <span className="text-xs text-gray-400">
                          Group {q.group_id} &middot; Type {q.question_type}
                        </span>
                        {edited && (
                          <span className="text-xs bg-amber-200 text-amber-800 px-1.5 py-0.5 rounded">
                            Edited
                          </span>
                        )}
                        {l1Edited && !edited && (
                          <span className="text-xs bg-blue-200 text-blue-800 px-1.5 py-0.5 rounded">
                            L1 Changed
                          </span>
                        )}
                      </div>
                      <p className="font-medium">{q.question_text}</p>
                      {l1Edited && q.l1_note && (
                        <p className="text-xs text-blue-700 bg-blue-50 border border-blue-200 rounded px-2 py-1 mt-1">
                          <span className="font-medium">L1 Note:</span> {q.l1_note}
                        </p>
                      )}
                    </div>

                    {/* Confidence */}
                    <div className="flex items-center gap-2 shrink-0">
                      <div className="w-20 bg-gray-200 rounded-full h-1.5">
                        <div
                          className={`h-1.5 rounded-full ${
                            (q.ai_confidence || 0) >= 90
                              ? "bg-green-500"
                              : (q.ai_confidence || 0) >= 70
                              ? "bg-amber-500"
                              : "bg-red-500"
                          }`}
                          style={{ width: `${q.ai_confidence || 0}%` }}
                        />
                      </div>
                      <span className="text-xs font-medium w-8 text-right">
                        {q.ai_confidence || 0}%
                      </span>
                    </div>
                  </div>

                  {/* Evidence */}
                  {q.ai_evidence && (
                    <blockquote className="border-l-4 border-blue-300 pl-3 text-xs italic text-gray-600 bg-blue-50 p-2 rounded">
                      {q.ai_evidence}
                    </blockquote>
                  )}

                  {/* Approval Bridge — how AI connected clinical notes to approval criteria */}
                  {q.ai_approval_gap && (
                    <div className="bg-green-50 border border-green-200 text-green-800 text-xs p-2 rounded space-y-1">
                      <div className="flex items-center gap-1.5 font-medium">
                        <span>🔗</span>
                        <span>Approval Bridge</span>
                      </div>
                      <p>{q.ai_approval_gap.replace(/^Missing:\s*/i, '')}</p>
                    </div>
                  )}

                  {/* Gap warning — only show if no approval_gap already covers it */}
                  {q.ai_gap && !q.ai_approval_gap && (
                    <div className="bg-amber-50 border border-amber-200 text-amber-700 text-xs p-2 rounded">
                      <span className="font-medium">Documentation gap:</span> {q.ai_gap.replace(/^Missing:\s*/i, '')}
                    </div>
                  )}

                  {/* Answer options */}
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-1.5">
                    {q.options_json?.map((opt: any) => {
                      const aiVal = getAiValue(q);
                      const isAiSelected = Array.isArray(aiVal)
                        ? aiVal.includes(opt.id)
                        : aiVal === opt.id;
                      const notesVal = q.ai_notes_answer?.Values || q.ai_notes_answer;
                      const isNotesSelected = notesVal
                        ? (Array.isArray(notesVal) ? notesVal.includes(opt.id) : notesVal === opt.id)
                        : false;
                      const hasNotesPath = q.ai_notes_answer != null && q.ai_approval_gap;
                      const isSelected = Array.isArray(currentAnswer)
                        ? currentAnswer.includes(opt.id)
                        : currentAnswer === opt.id;

                      return (
                        <label
                          key={opt.id}
                          className={`flex items-center gap-2 p-2 rounded border cursor-pointer text-sm transition-colors ${
                            isSelected && edited
                              ? "border-amber-500 bg-amber-100"
                              : isSelected
                              ? "border-blue-400 bg-blue-50"
                              : "border-gray-200 hover:border-gray-300"
                          }`}
                        >
                          <input
                            type={q.question_type === 4 ? "checkbox" : "radio"}
                            name={`answer-${q.group_id}`}
                            checked={isSelected}
                            onChange={() => {
                              if (q.question_type === 4) {
                                const current = Array.isArray(currentAnswer)
                                  ? currentAnswer
                                  : [];
                                if (current.includes(opt.id)) {
                                  setAnswer(
                                    q.group_id,
                                    current.filter(
                                      (id: string) => id !== opt.id
                                    )
                                  );
                                } else {
                                  setAnswer(q.group_id, [
                                    ...current,
                                    opt.id,
                                  ]);
                                }
                              } else {
                                setAnswer(q.group_id, opt.id);
                              }
                            }}
                            className="shrink-0"
                          />
                          <span className="flex-1">{opt.text}</span>
                          {isAiSelected && (
                            <span className="text-[10px] text-blue-500 font-medium">
                              AI
                            </span>
                          )}
                          {hasNotesPath && isNotesSelected && !isAiSelected && (
                            <span className="text-[10px] text-purple-500 font-medium">
                              Notes
                            </span>
                          )}
                          {hasNotesPath && isNotesSelected && isAiSelected && (
                            <span className="text-[10px] text-green-600 font-medium">
                              ✓
                            </span>
                          )}
                        </label>
                      );
                    })}
                  </div>

                  {/* AI Reasoning (collapsed) */}
                  {q.ai_reasoning && (
                    <details className="group">
                      <summary className="text-xs text-gray-400 cursor-pointer hover:text-gray-600">
                        ▸ AI reasoning
                      </summary>
                      <div className="text-xs text-gray-600 mt-1 pl-2 space-y-1">
                        <p><span className="font-medium text-blue-600">Approval:</span> {q.ai_reasoning}</p>
                        {q.ai_notes_reasoning && q.ai_approval_gap && (
                          <p><span className="font-medium text-purple-600">Notes:</span> {q.ai_notes_reasoning}</p>
                        )}
                      </div>
                    </details>
                  )}
                </div>
              );
            })
          )}
        </div>
      </div>
      )}
    </div>
    </>
  );
}
