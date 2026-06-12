import { AvailityCase } from "./types";

export function mapCaseToRow(item: any): AvailityCase {
  const er = item.eligibility_result;
  const firstPlan = er?.plans?.[0];

  return {
    _id: item._id?.toString() || "",

    claim_id: item.case_id || item.exam_id || item._id?.toString() || "",

    status: item.status || "",

    eligibility: item.eligibility_status || "",

    referral:
      item.payload?.ReferringProviderOfficeName ||
      `${item.payload?.ReferringProviderFirstName || ""} ${
        item.payload?.ReferringProviderLastName || ""
      }`.trim(),

    patient: er?.patient_name || "",

    dob: er?.date_of_birth || er?.patient?.date_of_birth || "",

    gender: er?.gender || er?.patient?.gender || "",

    member_id: er?.member_id || "",

    subscriber: `${er?.subscriber?.first_name || ""} ${
      er?.subscriber?.last_name || ""
    }`.trim(),

    payer: er?.payer_name || er?.payer?.name || "",

    payer_id: er?.payer_id || er?.payer?.payer_id || "",

    portal: item.payload?.PortalMatch || item.workflow_id || "",

    insurance: er?.payer_name || "",

    insurance_type: firstPlan?.insurance_type || "",

    group_number: firstPlan?.group_number || "",

    group_name: firstPlan?.group_name || "",

    plan_status: firstPlan?.status || "",

    coverage_start: firstPlan?.coverage_start_date || "",

    coverage_end: firstPlan?.coverage_end_date || "",

    auth_req: "",

    extracted: er ? "Yes" : "No",

    created_at: item.created_at,

    eligibility_result: er || null,
  };
}

export function badgeClass(value: string) {
  const normalized = value?.toLowerCase();

  if (normalized === "completed" || normalized === "complete") {
    return "bg-green-100 text-green-700";
  }

  if (normalized === "failed") {
    return "bg-red-100 text-red-700";
  }

  if (normalized === "submitted") {
    return "bg-blue-100 text-blue-700";
  }

  return "bg-gray-100 text-gray-700";
}