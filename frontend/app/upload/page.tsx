"use client";

import { useCallback, useState } from "react";
import { confirmUpload, uploadExcel } from "@/lib/api";

type CasePreview = {
  exam_id: string;
  first_name: string;
  last_name: string;
  cpt_code: string;
  icd1: string | null;
  center_abbr: string | null;
  state: string;
  is_stat: boolean;
  has_attachment: boolean;
  flags: string[];
};

type UploadResult = {
  filename: string;
  total_rows: number;
  duplicate_rows: number;
  unique_cases: number;
  stat_count: number;
  hold_count: number;
  cases_preview: CasePreview[];
  _parsed_cases: any[];
};

export default function UploadPage() {
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [uploading, setUploading] = useState(false);
  const [result, setResult] = useState<UploadResult | null>(null);
  const [batchId, setBatchId] = useState<string | null>(null);
  const [confirmResult, setConfirmResult] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);

  const handleDrop = useCallback(async (e: React.DragEvent) => {
    e.preventDefault();
    const file = e.dataTransfer.files[0];
    if (!file) return;
    await handleFile(file);
  }, []);

  const handleFileInput = useCallback(
    async (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (!file) return;
      await handleFile(file);
    },
    []
  );

  async function handleFile(file: File) {
    setUploading(true);
    setError(null);
    try {
      const data = await uploadExcel(file);
      setResult(data);
      setStep(2);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setUploading(false);
    }
  }

  async function handleConfirm() {
    if (!result) return;
    setUploading(true);
    try {
      const data: any = await confirmUpload({
        cases: result._parsed_cases,
        filename: result.filename,
        uploaded_by: "rep",
        total_rows: result.total_rows,
        duplicate_rows: result.duplicate_rows,
      });
      setBatchId(data.batch_id);
      setConfirmResult(data);
      setStep(3);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setUploading(false);
    }
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Upload Cases</h1>

      {/* Step indicators */}
      <div className="flex gap-4 text-sm">
        <span className={step >= 1 ? "font-bold text-blue-600" : "text-gray-400"}>
          1. Upload Excel
        </span>
        <span className={step >= 2 ? "font-bold text-blue-600" : "text-gray-400"}>
          2. Review & Confirm
        </span>
        <span className={step >= 3 ? "font-bold text-blue-600" : "text-gray-400"}>
          3. Process
        </span>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded">
          {error}
        </div>
      )}

      {/* Step 1: Upload */}
      {step === 1 && (
        <div
          onDragOver={(e) => e.preventDefault()}
          onDrop={handleDrop}
          className="border-2 border-dashed rounded-lg p-12 text-center"
        >
          {uploading ? (
            <p className="text-gray-500">Parsing Excel file...</p>
          ) : (
            <>
              <p className="text-gray-600 mb-4">
                Drag & drop a Carelon Excel file, or click to browse
              </p>
              <label className="cursor-pointer bg-blue-600 text-white px-4 py-2 rounded hover:bg-blue-700">
                Choose File
                <input
                  type="file"
                  accept=".xlsx,.xls"
                  onChange={handleFileInput}
                  className="hidden"
                />
              </label>
            </>
          )}
        </div>
      )}

      {/* Step 2: Preview */}
      {step === 2 && result && (
        <div className="space-y-4">
          <div className="bg-blue-50 border border-blue-200 rounded p-4">
            <p className="font-semibold">{result.filename}</p>
            <p className="text-sm text-gray-600">
              {result.total_rows} rows → {result.unique_cases} unique cases
              ({result.duplicate_rows} duplicates removed)
            </p>
            <div className="flex gap-4 mt-2 text-sm">
              {result.stat_count > 0 && (
                <span className="text-red-600 font-medium">
                  {result.stat_count} STAT
                </span>
              )}
              <span className="text-green-600">
                {result.unique_cases - result.hold_count} Ready
              </span>
              {result.hold_count > 0 && (
                <span className="text-amber-600">{result.hold_count} On hold</span>
              )}
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-sm border">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-3 py-2 text-left">Status</th>
                  <th className="px-3 py-2 text-left">ExamId</th>
                  <th className="px-3 py-2 text-left">Patient</th>
                  <th className="px-3 py-2 text-left">CPT</th>
                  <th className="px-3 py-2 text-left">ICD</th>
                  <th className="px-3 py-2 text-left">Center</th>
                  <th className="px-3 py-2 text-left">Flags</th>
                </tr>
              </thead>
              <tbody>
                {result.cases_preview.map((c) => (
                  <tr key={c.exam_id} className="border-t">
                    <td className="px-3 py-2">
                      <StateBadge state={c.state} isStat={c.is_stat} />
                    </td>
                    <td className="px-3 py-2 font-mono text-xs">{c.exam_id}</td>
                    <td className="px-3 py-2">
                      {c.first_name} {c.last_name}
                    </td>
                    <td className="px-3 py-2">{c.cpt_code}</td>
                    <td className="px-3 py-2">{c.icd1 || "—"}</td>
                    <td className="px-3 py-2">{c.center_abbr || "—"}</td>
                    <td className="px-3 py-2">
                      {c.has_attachment && <span title="RIS doc attached">📎</span>}
                      {c.flags.map((f) => (
                        <span key={f} className="text-xs text-amber-600 ml-1">
                          {f}
                        </span>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <button
            onClick={handleConfirm}
            disabled={uploading}
            className="bg-blue-600 text-white px-6 py-2 rounded hover:bg-blue-700 disabled:opacity-50"
          >
            {uploading ? "Creating cases..." : "Confirm & Create Cases"}
          </button>
        </div>
      )}

      {/* Step 3: Done */}
      {step === 3 && batchId && (
        <div className="bg-green-50 border border-green-200 rounded p-6 text-center">
          <p className="text-green-700 font-semibold text-lg mb-2">
            {confirmResult?.unique_cases > 0
              ? `${confirmResult.unique_cases} cases created successfully`
              : "All cases already exist in the system"}
          </p>
          {confirmResult?.skipped_duplicates > 0 && (
            <p className="text-sm text-amber-600 mb-2">
              {confirmResult.skipped_duplicates} duplicate cases skipped
            </p>
          )}
          <p className="text-sm text-gray-600 mb-4">
            Batch ID: {batchId}
          </p>
          <div className="flex gap-4 justify-center">
            <a
              href="/cases"
              className="bg-blue-600 text-white px-4 py-2 rounded hover:bg-blue-700"
            >
              View Cases
            </a>
            <button
              onClick={() => {
                setStep(1);
                setResult(null);
                setBatchId(null);
                setConfirmResult(null);
              }}
              className="border px-4 py-2 rounded hover:bg-gray-50"
            >
              Upload Another
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function StateBadge({ state, isStat }: { state: string; isStat: boolean }) {
  if (isStat) {
    return (
      <span className="bg-red-100 text-red-700 text-xs px-2 py-0.5 rounded font-medium">
        STAT
      </span>
    );
  }
  if (state === "HOLD") {
    return (
      <span className="bg-amber-100 text-amber-700 text-xs px-2 py-0.5 rounded">
        HOLD
      </span>
    );
  }
  return (
    <span className="bg-green-100 text-green-700 text-xs px-2 py-0.5 rounded">
      READY
    </span>
  );
}
