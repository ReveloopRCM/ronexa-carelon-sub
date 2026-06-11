export type AvailityWorklistFilter = {
  key: string;
  label: string;
  count: number;
};

export type AvailityWorklistRow = {
  job_id: string;
  case_id: string;
  exam_id: string;
  status: string;
  exception_type: string;
  exception_detail?: string | null;
  created_at?: string | null;
};

export type AvailityWorklistResponse = {
  rows: AvailityWorklistRow[];
  count: number;
  total: number;
  page: number;
  page_size: number;
};