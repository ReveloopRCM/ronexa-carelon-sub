import { EligibilityResult } from "../types";

export default function EligibilityDetail({
  result,
}: {
  result: EligibilityResult | null;
}) {
  if (!result) {
    return (
      <p className="text-xs text-gray-500">
        No eligibility result available.
      </p>
    );
  }

  return (
    <div className="bg-white border rounded p-3">
      <p className="text-xs font-semibold text-gray-700 mb-2">
        Eligibility Result JSON
      </p>

      <pre className="text-[10px] bg-gray-50 p-3 rounded overflow-auto max-h-96 text-gray-700">
        {JSON.stringify(result, null, 2)}
      </pre>
    </div>
  );
}