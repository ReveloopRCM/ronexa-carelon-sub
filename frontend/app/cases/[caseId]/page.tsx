"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { getCase, processCase, uploadNotes, resolveClinicals, cureAndRequeue, requeueCase } from "@/lib/api";

export default function CaseDetailPage() {
  const params = useParams();
  const caseId = params.caseId as string;
  const [caseData, setCaseData] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  // Upload state
  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState<any>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    loadCase();
  }, [caseId]);

  async function loadCase() {
    try {
      const data = await getCase(caseId);
      setCaseData(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }

  async function handleProcess() {
    try {
      await processCase(caseId);
      await loadCase();
    } catch (err: any) {
      alert(err.message);
    }
  }

  const handleUpload = useCallback(
    async (file: File) => {
      if (!file.name.toLowerCase().endsWith(".pdf")) {
        setUploadError("Only PDF files are supported");
        return;
      }
      setUploading(true);
      setUploadError(null);
      setUploadResult(null);
      try {
        const result = await uploadNotes(caseId, file);
        setUploadResult(result);
        await loadCase(); // Refresh case data (state + clinical_notes)
      } catch (err: any) {
        setUploadError(err.message || "Upload failed");
      } finally {
        setUploading(false);
      }
    },
    [caseId]
  );

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files?.[0];
    if (file) handleUpload(file);
  }

  function handleFileSelect(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (file) handleUpload(file);
    // Reset so same file can be re-selected
    e.target.value = "";
  }

  if (loading) return <p className="text-gray-500">Loading...</p>;
  if (!caseData) return <p className="text-red-500">Case not found</p>;

  const notes = caseData.clinical_notes || [];
  const hasNotes = notes.length > 0;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Case: {caseData.exam_id}</h1>
        <span className="text-sm px-3 py-1 rounded bg-gray-100">
          {caseData.state}
        </span>
      </div>

      {/* Case Info */}
      <div className="grid grid-cols-2 gap-6">
        <div className="border rounded p-4 space-y-2">
          <h2 className="font-semibold">Patient</h2>
          <p>
            {caseData.first_name} {caseData.last_name}
          </p>
          <p className="text-sm text-gray-600">DOB: {caseData.dob}</p>
          <p className="text-sm text-gray-600">
            Policy: {caseData.policy_num}
          </p>
        </div>
        <div className="border rounded p-4 space-y-2">
          <h2 className="font-semibold">Procedure</h2>
          <p>CPT: {caseData.cpt_code}</p>
          <p className="text-sm text-gray-600">
            ICD: {caseData.icd1 || "\u2014"}
          </p>
          <p className="text-sm text-gray-600">
            Center: {caseData.center_abbr || caseData.center_npi}
          </p>
        </div>
      </div>

      {/* Auth Result */}
      {caseData.auth_number && (
        <div className="bg-green-50 border border-green-200 rounded p-4 flex items-center justify-between">
          <div>
            <p className="font-semibold text-green-700">
              Approved — Auth #: {caseData.auth_number}
            </p>
            {caseData.valid_from && caseData.valid_through && (
              <p className="text-sm text-green-600">
                Valid: {caseData.valid_from} – {caseData.valid_through}
              </p>
            )}
          </div>
          {caseData.auth_pdf_url && (
            <a
              href={`${process.env.NEXT_PUBLIC_API_URL || ""}/api/cases/${caseData.id}/auth-pdf`}
              target="_blank"
              rel="noopener noreferrer"
              className="px-3 py-1.5 bg-green-600 text-white text-sm rounded hover:bg-green-700"
            >
              View Auth Confirmation
            </a>
          )}
        </div>
      )}

      {/* Auth Confirmation Screenshot */}
      {caseData.auth_pdf_url && caseData.auth_number && (
        <div className="border rounded-lg overflow-hidden bg-gray-50">
          <p className="text-xs text-gray-500 px-3 py-2 border-b bg-white font-medium">
            Portal Auth Confirmation
          </p>
          <img
            src={`${process.env.NEXT_PUBLIC_API_URL || ""}/api/cases/${caseData.id}/auth-pdf`}
            alt="Authorization confirmation from portal"
            className="w-full"
          />
        </div>
      )}

      {caseData.denial_reason && (
        <div className="bg-red-50 border border-red-200 rounded p-4">
          <p className="font-semibold text-red-700">Denied</p>
          <p className="text-sm">{caseData.denial_reason}</p>
        </div>
      )}

      {/* Hold Reason Banner + Cure & Requeue */}
      {caseData.state === "HOLD" && <HoldCurePanel caseData={caseData} onCured={loadCase} />}

      {/* Failed Banner + Requeue Button */}
      {caseData.state === "FAILED" && (
        <div className="bg-red-50 border border-red-300 rounded p-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="font-semibold text-red-700">Failed</p>
              <p className="text-sm text-red-600">{caseData.hold_reason || "Processing failed after max retries"}</p>
            </div>
            <button
              onClick={async () => {
                try {
                  const { requeueCase } = await import("@/lib/api");
                  await requeueCase(caseData.id);
                  window.location.reload();
                } catch {}
              }}
              className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700"
            >
              Retry Case
            </button>
          </div>
        </div>
      )}

      {/* Waiting for Clinicals Banner */}
      {caseData.state === "WAITING_CLINICALS" && (
        <div className="bg-amber-50 border border-amber-300 rounded p-4 space-y-3">
          <div className="flex items-center gap-2">
            <svg className="w-5 h-5 text-amber-500 animate-pulse" fill="currentColor" viewBox="0 0 20 20">
              <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm1-12a1 1 0 10-2 0v4a1 1 0 00.293.707l2.828 2.829a1 1 0 101.415-1.415L11 9.586V6z" clipRule="evenodd" />
            </svg>
            <p className="font-semibold text-amber-700">Waiting for Clinical Documents</p>
          </div>
          <p className="text-sm text-amber-600">
            Workflow is suspended — no clinical notes were available when processing started.
            Upload the clinical PDF below, or click &quot;Clinicals Ready&quot; if they&apos;ve been synced.
          </p>
          {hasNotes && (
            <button
              onClick={async () => {
                try {
                  await resolveClinicals(caseId);
                  await loadCase();
                } catch (err: any) {
                  alert(err.message);
                }
              }}
              className="bg-amber-600 text-white px-4 py-2 rounded hover:bg-amber-700 text-sm"
            >
              Clinicals Ready — Resume Workflow
            </button>
          )}
        </div>
      )}

      {/* Flow Checks — Inline Intelligence Steps */}
      {caseData.flow_checks?.length > 0 && (
        <div className="space-y-3">
          <h2 className="font-semibold text-lg">Flow Checks</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {caseData.flow_checks.map((fc: any, idx: number) => {
              const checkName = fc.check;
              const data = fc.data || {};

              // Determine pass/fail status and display info per check type
              let status: "pass" | "fail" | "info" = "info";
              let label = checkName;
              let detail = "";

              if (checkName === "clinical_context") {
                label = "Clinical Notes";
                status = data.has_notes ? "pass" : "fail";
                detail = data.has_notes
                  ? `Loaded: ${(data.fields || []).slice(0, 5).join(", ")}`
                  : "No clinical notes available";
              } else if (checkName === "eligibility") {
                label = "Eligibility";
                status = data.eligible ? "pass" : "fail";
                detail = data.eligible
                  ? `Effective: ${data.effective || "?"} \u2014 ${data.term || "no term"}`
                  : data.reason || "Not eligible";
              } else if (checkName === "duplicate_auth") {
                label = "Duplicate Auth";
                status = !data.duplicate ? "pass" : "fail";
                detail = data.duplicate
                  ? data.reason || "Duplicate found"
                  : "No duplicate authorizations";
              } else if (checkName === "provider_match") {
                label = "Referring Provider";
                const method = data.match_method || data.method;
                if (method === "address") {
                  status = "pass";
                  detail = `${data.provider_name || data.selected_name} — address matched`;
                } else if (method === "name") {
                  status = "pass";
                  detail = `${data.provider_name || data.selected_name} — name matched`;
                } else if (method === "single_result") {
                  status = "pass";
                  detail = `${data.provider_name || data.selected_name} — single result`;
                } else {
                  status = "info";
                  detail = `${data.provider_name || data.selected_name} — default selection`;
                }
                // Add RIS data to detail
                const extras = [];
                if (data.ris_name) extras.push(`RIS: ${data.ris_name}`);
                if (data.ris_address || data.provider_address) extras.push(`Addr: ${data.ris_address || data.provider_address}`);
                if (data.fax_entered) extras.push(`Fax: ${data.fax_entered}`);
                if (data.results_count) extras.push(`${data.results_count} portal results`);
                if (extras.length > 0) detail += ` | ${extras.join(" | ")}`;
              } else if (checkName === "contrast_selection") {
                label = "Contrast";
                status = "pass";
                detail = `CPT ${data.cpt_code}: ${data.final_contrast || data.contrast || "?"}`;
              } else if (checkName === "completeness") {
                label = "Completeness Gate";
                status = data.passed ? "pass" : "fail";
                detail = data.passed
                  ? `${data.total_answers || 0} answers, ${data.low_confidence_count || 0} low-conf`
                  : data.halt_reason || "Failed";
              }

              const colors =
                status === "pass"
                  ? "bg-green-50 border-green-200"
                  : status === "fail"
                  ? "bg-red-50 border-red-200"
                  : "bg-blue-50 border-blue-200";

              const iconColor =
                status === "pass"
                  ? "text-green-600"
                  : status === "fail"
                  ? "text-red-600"
                  : "text-blue-600";

              return (
                <div
                  key={idx}
                  className={`border rounded p-3 ${colors}`}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <span className={`text-lg ${iconColor}`}>
                      {status === "pass" ? "\u2713" : status === "fail" ? "\u2717" : "\u2139"}
                    </span>
                    <span className="font-medium text-sm">{label}</span>
                  </div>
                  <p className="text-xs text-gray-600">{detail}</p>
                  {fc.timestamp && (
                    <p className="text-xs text-gray-400 mt-1">
                      {new Date(fc.timestamp).toLocaleString()}
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Extracted Clinical Data — for QA review */}
      <div className="space-y-3">
        <h2 className="font-semibold text-lg">Extracted Clinical Data</h2>

        {hasNotes ? (
          notes.map((n: any) => {
            const s = n.structured || {};
            const hasStructured = Object.keys(s).length > 0 && !s.error;

            return (
              <div key={n.id} className="border rounded overflow-hidden">
                {/* Note header with PDF link */}
                <div className="flex items-center justify-between bg-gray-50 px-4 py-3 border-b">
                  <div className="flex items-center gap-3">
                    <svg className="w-5 h-5 text-red-500 shrink-0" fill="currentColor" viewBox="0 0 20 20">
                      <path d="M4 18h12a2 2 0 002-2V6l-4-4H4a2 2 0 00-2 2v12a2 2 0 002 2zm10-16l4 4h-4V2z" />
                    </svg>
                    <div>
                      <p className="text-sm font-medium">
                        {n.page_count} page{n.page_count !== 1 ? "s" : ""} &middot; {n.document_type}
                      </p>
                      <p className="text-xs text-gray-400">
                        {n.uploaded_at ? new Date(n.uploaded_at).toLocaleString() : ""}
                      </p>
                    </div>
                  </div>
                  {(caseData.clinical_blob_key || caseData.file_key) && (
                    <a
                      href={`/api/cases/${caseId}/clinical-pdf`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-sm text-blue-600 hover:text-blue-800 flex items-center gap-1"
                    >
                      <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                      </svg>
                      View PDF
                    </a>
                  )}
                </div>

                {/* Structured extraction data */}
                {hasStructured ? (
                  <div className="px-4 py-3 space-y-3">
                    {/* OCR Text from Document Intelligence */}
                    {s.text && (
                      <div>
                        <div className="flex items-center justify-between mb-2">
                          <span className="text-gray-400 text-xs uppercase tracking-wide">
                            OCR Text ({Math.round(s.text.length / 1000)}K chars)
                          </span>
                          {s._meta?.page_count && (
                            <span className="text-xs text-gray-400">{s._meta.page_count} pages</span>
                          )}
                        </div>
                        <div className="bg-gray-50 rounded p-3 max-h-96 overflow-y-auto">
                          <pre className="text-xs text-gray-700 whitespace-pre-wrap font-mono leading-relaxed">
                            {s.text}
                          </pre>
                        </div>
                        {s.tables?.length > 0 && (
                          <p className="text-xs text-gray-400 mt-1">
                            {s.tables.length} table{s.tables.length !== 1 ? "s" : ""} extracted
                          </p>
                        )}
                      </div>
                    )}

                    {/* Key clinical fields in a grid (from legacy vision extraction) */}
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-2 text-sm">
                      {s.patient_name && (
                        <div>
                          <span className="text-gray-400 text-xs uppercase tracking-wide">Patient</span>
                          <p>{s.patient_name}{s.dob ? ` (DOB: ${s.dob})` : ""}</p>
                        </div>
                      )}
                      {s.provider_name && (
                        <div>
                          <span className="text-gray-400 text-xs uppercase tracking-wide">Provider</span>
                          <p>{s.provider_name}{s.provider_npi ? ` (NPI: ${s.provider_npi})` : ""}</p>
                        </div>
                      )}
                      {s.date_of_service && (
                        <div>
                          <span className="text-gray-400 text-xs uppercase tracking-wide">Date of Service</span>
                          <p>{s.date_of_service}</p>
                        </div>
                      )}
                      {s.chief_complaint && (
                        <div>
                          <span className="text-gray-400 text-xs uppercase tracking-wide">Chief Complaint</span>
                          <p>{s.chief_complaint}</p>
                        </div>
                      )}
                      {s.body_part && (
                        <div>
                          <span className="text-gray-400 text-xs uppercase tracking-wide">Body Part</span>
                          <p>{s.body_part}{s.laterality ? ` (${s.laterality})` : ""}</p>
                        </div>
                      )}
                      {s.symptom_duration && (
                        <div>
                          <span className="text-gray-400 text-xs uppercase tracking-wide">Symptom Duration</span>
                          <p>{s.symptom_duration}</p>
                        </div>
                      )}
                    </div>

                    {/* Clinical Indication — full width, highlighted */}
                    {s.clinical_indication && (
                      <div className="bg-blue-50 border border-blue-100 rounded p-3">
                        <span className="text-gray-500 text-xs uppercase tracking-wide">Clinical Indication</span>
                        <p className="text-sm mt-0.5">{s.clinical_indication}</p>
                      </div>
                    )}

                    {/* Diagnoses */}
                    {s.diagnoses?.length > 0 && (
                      <div>
                        <span className="text-gray-400 text-xs uppercase tracking-wide">Diagnoses</span>
                        <ul className="text-sm mt-0.5 list-disc list-inside">
                          {s.diagnoses.map((d: string, i: number) => (
                            <li key={i}>{d}</li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {/* Findings */}
                    {s.findings?.length > 0 && (
                      <div>
                        <span className="text-gray-400 text-xs uppercase tracking-wide">Findings</span>
                        <ul className="text-sm mt-0.5 list-disc list-inside text-gray-700">
                          {s.findings.map((f: string, i: number) => (
                            <li key={i}>{f}</li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {/* Procedures Ordered */}
                    {s.procedures_ordered?.length > 0 && (
                      <div>
                        <span className="text-gray-400 text-xs uppercase tracking-wide">Procedures Ordered</span>
                        <ul className="text-sm mt-0.5 list-disc list-inside">
                          {s.procedures_ordered.map((p: string, i: number) => (
                            <li key={i}>{p}</li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {/* Medications */}
                    {s.medications?.length > 0 && (
                      <div>
                        <span className="text-gray-400 text-xs uppercase tracking-wide">Medications</span>
                        <ul className="text-sm mt-0.5 list-disc list-inside text-gray-700">
                          {s.medications.map((m: string, i: number) => (
                            <li key={i}>{m}</li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {/* Prior Treatments */}
                    {s.prior_treatments?.length > 0 && (
                      <div>
                        <span className="text-gray-400 text-xs uppercase tracking-wide">Prior Treatments</span>
                        <ul className="text-sm mt-0.5 list-disc list-inside text-gray-700">
                          {s.prior_treatments.map((t: string, i: number) => (
                            <li key={i}>{t}</li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {/* Prior Imaging */}
                    {s.prior_imaging?.length > 0 && (
                      <div>
                        <span className="text-gray-400 text-xs uppercase tracking-wide">Prior Imaging</span>
                        <ul className="text-sm mt-0.5 list-disc list-inside text-gray-700">
                          {s.prior_imaging.map((img: string, i: number) => (
                            <li key={i}>{img}</li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {/* Relevant History */}
                    {s.relevant_history && (
                      <div>
                        <span className="text-gray-400 text-xs uppercase tracking-wide">Relevant History</span>
                        <p className="text-sm mt-0.5 text-gray-700">{s.relevant_history}</p>
                      </div>
                    )}

                    {/* Raw Text Excerpts — for QA verification */}
                    {s.raw_text_excerpts?.length > 0 && (
                      <details className="group">
                        <summary className="text-gray-400 text-xs uppercase tracking-wide cursor-pointer hover:text-gray-600">
                          Raw Text Excerpts ({s.raw_text_excerpts.length})
                        </summary>
                        <div className="mt-1 bg-gray-50 rounded p-2 space-y-1">
                          {s.raw_text_excerpts.map((t: string, i: number) => (
                            <p key={i} className="text-xs text-gray-600 font-mono">&quot;{t}&quot;</p>
                          ))}
                        </div>
                      </details>
                    )}
                  </div>
                ) : s.error ? (
                  <div className="px-4 py-3">
                    <p className="text-sm text-amber-600">
                      Extraction failed: {s.error}
                    </p>
                  </div>
                ) : (
                  <div className="px-4 py-3">
                    <p className="text-sm text-gray-400">No structured data extracted</p>
                  </div>
                )}
              </div>
            );
          })
        ) : (
          /* No notes yet — show PDF link if blob exists + upload area */
          (caseData.clinical_blob_key || caseData.file_key) ? (
            <div className="border rounded p-4 flex items-center justify-between">
              <p className="text-sm text-gray-500">Clinical PDF available but not yet extracted</p>
              <a
                href={`/api/cases/${caseId}/clinical-pdf`}
                target="_blank"
                rel="noopener noreferrer"
                className="text-sm text-blue-600 hover:text-blue-800 flex items-center gap-1"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                </svg>
                View PDF
              </a>
            </div>
          ) : null
        )}

        {/* Upload area */}
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
          onClick={() => fileInputRef.current?.click()}
          className={`border-2 border-dashed rounded-lg p-6 text-center cursor-pointer transition-colors ${
            dragOver
              ? "border-blue-400 bg-blue-50"
              : "border-gray-300 hover:border-gray-400"
          } ${uploading ? "opacity-50 pointer-events-none" : ""}`}
        >
          <input
            ref={fileInputRef}
            type="file"
            accept=".pdf"
            onChange={handleFileSelect}
            className="hidden"
          />
          {uploading ? (
            <div className="space-y-2">
              <div className="animate-spin w-6 h-6 border-2 border-blue-500 border-t-transparent rounded-full mx-auto" />
              <p className="text-sm text-gray-600">
                Uploading & extracting clinical data...
              </p>
            </div>
          ) : (
            <div className="space-y-1">
              <p className="text-sm text-gray-600">
                {hasNotes
                  ? "Drop another PDF to add more notes"
                  : "Drop a clinical note PDF here, or click to browse"}
              </p>
              <p className="text-xs text-gray-400">PDF files only</p>
            </div>
          )}
        </div>

        {/* Upload error */}
        {uploadError && (
          <div className="bg-red-50 border border-red-200 rounded p-3">
            <p className="text-sm text-red-700">{uploadError}</p>
          </div>
        )}
      </div>

      {/* Actions */}
      {["PENDING_NOTES", "PENDING_STAT", "NOTES_UPLOADED"].includes(
        caseData.state
      ) && (
        <button
          onClick={handleProcess}
          className="bg-blue-600 text-white px-4 py-2 rounded hover:bg-blue-700"
        >
          Process Case
        </button>
      )}
      {caseData.state === "FAILED" && (
        <button
          onClick={handleProcess}
          className="bg-amber-600 text-white px-4 py-2 rounded hover:bg-amber-700"
        >
          Retry
        </button>
      )}

      {/* Questions */}
      {caseData.questions?.length > 0 && (
        <div className="space-y-3">
          <h2 className="font-semibold text-lg">Questions</h2>
          {caseData.questions.map((q: any) => (
            <div key={q.id} className="border rounded p-4 space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-xs bg-gray-100 px-2 py-0.5 rounded">
                  Q{q.sequence} / Type {q.question_type}
                </span>
                <span className="text-xs">{q.review_state}</span>
              </div>
              <p className="font-medium">{q.question_text}</p>
              {q.ai_evidence && (
                <blockquote className="text-sm text-gray-600 border-l-2 pl-3 italic">
                  {q.ai_evidence}
                </blockquote>
              )}
              <div className="flex gap-4 text-sm">
                <span>
                  AI: <code>{JSON.stringify(q.ai_answer)}</code>
                </span>
                <span>Confidence: {q.ai_confidence}%</span>
              </div>
              {q.rep_answer && (
                <p className="text-sm text-blue-600">
                  Rep answer: <code>{JSON.stringify(q.rep_answer)}</code>
                </p>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Audit Trail */}
      {caseData.audit_trail?.length > 0 && (
        <div className="space-y-2">
          <h2 className="font-semibold text-lg">Audit Trail</h2>
          <div className="text-sm space-y-1">
            {caseData.audit_trail.map((e: any) => (
              <div key={e.id} className="flex gap-3 text-gray-600">
                <span className="text-xs text-gray-400 w-40 shrink-0">
                  {e.timestamp
                    ? new Date(e.timestamp).toLocaleString()
                    : "\u2014"}
                </span>
                <span className="font-mono text-xs">{e.actor}</span>
                <span>{e.action}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}


/* ── Hold Cure Panel ── */

const CURABLE_FIELDS: { key: string; label: string }[] = [
  { key: "first_name", label: "First Name" },
  { key: "last_name", label: "Last Name" },
  { key: "dob", label: "Date of Birth" },
  { key: "policy_num", label: "Policy Number" },
  { key: "center_npi", label: "Center NPI" },
  { key: "center_abbr", label: "Center Abbr" },
  { key: "patient_zip", label: "Patient ZIP" },
  { key: "cpt_code", label: "CPT Code" },
  { key: "icd1", label: "ICD-10 Primary" },
  { key: "icd2", label: "ICD-10 #2" },
  { key: "icd3", label: "ICD-10 #3" },
  { key: "icd4", label: "ICD-10 #4" },
  { key: "icd5", label: "ICD-10 #5" },
  { key: "referring_npi", label: "Referring NPI" },
  { key: "patient_phone", label: "Patient Phone" },
  { key: "referring_fax", label: "Referring Fax" },
  { key: "carrier_id", label: "Carrier ID" },
];

function HoldCurePanel({ caseData, onCured }: { caseData: any; onCured: () => void }) {
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const hasEdits = Object.keys(edits).length > 0;

  function handleFieldChange(key: string, value: string) {
    const original = caseData[key] || "";
    if (value === original) {
      // Remove edit if reverted to original
      const next = { ...edits };
      delete next[key];
      setEdits(next);
    } else {
      setEdits({ ...edits, [key]: value });
    }
  }

  async function handleCureAndRequeue() {
    setSaving(true);
    setError(null);
    try {
      await cureAndRequeue(caseData.id, edits);
      onCured();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function handleRequeue() {
    setSaving(true);
    setError(null);
    try {
      await requeueCase(caseData.id);
      onCured();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="bg-amber-50 border border-amber-300 rounded p-4 space-y-4">
      <div>
        <p className="font-semibold text-amber-700">On Hold</p>
        <p className="text-sm text-amber-600">{caseData.hold_reason}</p>
      </div>

      {/* Editable fields grid */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
        {CURABLE_FIELDS.map(({ key, label }) => {
          const current = caseData[key] || "";
          const isEdited = key in edits;
          return (
            <div key={key}>
              <label className="text-xs text-gray-500 block mb-0.5">{label}</label>
              <input
                type="text"
                defaultValue={current}
                onChange={(e) => handleFieldChange(key, e.target.value)}
                className={`w-full text-sm border rounded px-2 py-1.5 ${
                  isEdited
                    ? "border-blue-400 bg-blue-50 ring-1 ring-blue-200"
                    : "border-gray-300"
                }`}
                placeholder={`Enter ${label.toLowerCase()}`}
              />
            </div>
          );
        })}
      </div>

      {error && (
        <p className="text-sm text-red-600">{error}</p>
      )}

      {/* Actions */}
      <div className="flex gap-3">
        {hasEdits ? (
          <button
            onClick={handleCureAndRequeue}
            disabled={saving}
            className="px-4 py-2 bg-green-600 text-white rounded text-sm hover:bg-green-700 disabled:opacity-50"
          >
            {saving ? "Saving..." : `Cure & Requeue (${Object.keys(edits).length} field${Object.keys(edits).length > 1 ? "s" : ""} changed)`}
          </button>
        ) : (
          <button
            onClick={handleRequeue}
            disabled={saving}
            className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50"
          >
            {saving ? "Requeuing..." : "Requeue for Processing"}
          </button>
        )}
      </div>
    </div>
  );
}
