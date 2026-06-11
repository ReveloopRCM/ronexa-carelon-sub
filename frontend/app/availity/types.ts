export type EligibilityResult = Record<string, any>;

export type AvailityCase = {
  _id: string;
  claim_id: string;
  status: string;
  eligibility: string;
  referral: string;
  patient: string;
  dob: string;
  gender: string;
  member_id: string;
  subscriber: string;
  payer: string;
  payer_id: string;
  portal: string;
  insurance: string;
  insurance_type: string;
  group_number: string;
  group_name: string;
  plan_status: string;
  coverage_start: string;
  coverage_end: string;
  auth_req: string;
  extracted: string;
  created_at?: string;
  eligibility_result: EligibilityResult | null;
};

export type FetchAvailityResponse = {
  items: AvailityCase[];
  total: number;
  page: number;
  pageSize: number;
};