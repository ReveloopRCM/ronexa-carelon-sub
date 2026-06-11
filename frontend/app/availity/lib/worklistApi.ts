import type {
  AvailityWorklistFilter,
  AvailityWorklistResponse,
} from "./worklistTypes";

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000";

async function apiFetch<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
  });

  if (!res.ok) {
    throw new Error(`Request failed: ${res.status}`);
  }

  return res.json();
}

export async function getAvailityWorklistFilters() {
  return apiFetch<AvailityWorklistFilter[]>("/api/availity/worklist/filters");
}

export async function listAvailityWorklist(params?: {
  exceptionType?: string;
  page?: number;
  pageSize?: number;
}) {
  const searchParams = new URLSearchParams();

  if (params?.exceptionType) {
    searchParams.set("exception_type", params.exceptionType);
  }

  searchParams.set("page", String(params?.page ?? 1));
  searchParams.set("page_size", String(params?.pageSize ?? 200));

  return apiFetch<AvailityWorklistResponse>(
    `/api/availity/worklist?${searchParams.toString()}`
  );
}