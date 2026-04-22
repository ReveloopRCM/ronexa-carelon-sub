const API_BASE = process.env.NEXT_PUBLIC_API_URL
  ? `${process.env.NEXT_PUBLIC_API_URL}/api`
  : "/api";

async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...options?.headers },
    ...options,
  });
  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: res.statusText }));
    const detail = Array.isArray(error.detail)
      ? error.detail.map((d: any) => d.msg || JSON.stringify(d)).join("; ")
      : error.detail;
    throw new Error(detail || `API error: ${res.status}`);
  }
  return res.json();
}

export async function uploadExcel(file: File) {
  const formData = new FormData();
  formData.append("file", file);
  const res = await fetch(`${API_BASE}/upload/excel`, {
    method: "POST",
    body: formData,
  });
  if (!res.ok) throw new Error("Upload failed");
  return res.json();
}

export async function confirmUpload(data: {
  cases: any[];
  filename: string;
  uploaded_by: string;
  total_rows: number;
  duplicate_rows: number;
}) {
  return apiFetch("/upload/confirm", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function listCases(params?: {
  state?: string;
  center_npi?: string;
  batch_id?: string;
  date_from?: string;
  date_to?: string;
  limit?: number;
  offset?: number;
}) {
  const searchParams = new URLSearchParams();
  if (params?.state) searchParams.set("state", params.state);
  if (params?.center_npi) searchParams.set("center_npi", params.center_npi);
  if (params?.batch_id) searchParams.set("batch_id", params.batch_id);
  if (params?.date_from) searchParams.set("date_from", params.date_from);
  if (params?.date_to) searchParams.set("date_to", params.date_to);
  if (params?.limit) searchParams.set("limit", String(params.limit));
  if (params?.offset) searchParams.set("offset", String(params.offset));
  const qs = searchParams.toString();
  return apiFetch<any[]>(`/cases${qs ? `?${qs}` : ""}`);
}

export async function getCase(caseId: string) {
  return apiFetch<any>(`/cases/${caseId}`);
}

export async function processCase(caseId: string) {
  return apiFetch(`/cases/${caseId}/process`, { method: "POST" });
}

export async function requeueCase(caseId: string) {
  return apiFetch(`/cases/${caseId}/requeue`, { method: "POST" });
}

export async function cureAndRequeue(caseId: string, updates: Record<string, string>) {
  return apiFetch(`/cases/${caseId}/cure`, {
    method: "PATCH",
    body: JSON.stringify(updates),
  });
}

export async function listQueue(limit = 50, level?: number | string, source?: string) {
  const params = new URLSearchParams({ limit: String(limit) });
  if (level) params.set("level", String(level));
  if (source) params.set("source", source);
  return apiFetch<any[]>(`/queue?${params}`);
}

export async function getQueueItem(caseId: string) {
  return apiFetch<any>(`/queue/${caseId}`);
}

export async function submitBatchReview(
  caseId: string,
  data: {
    rep_id: string;
    answers: { group_id: number; answer_value: string | string[] }[];
    note?: string;
  }
) {
  return apiFetch<any>(`/queue/${caseId}/resolve`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function resolveL1(
  caseId: string,
  data: {
    rep_id: string;
    answers: { group_id: number; answer_value: string | string[] }[];
    note?: string;
  }
) {
  return apiFetch<any>(`/queue/${caseId}/resolve-l1`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function resolveL2(
  caseId: string,
  data: {
    rep_id: string;
    answers: { group_id: number; answer_value: string | string[] }[];
    note?: string;
  }
) {
  return apiFetch<any>(`/queue/${caseId}/resolve-l2`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function rerunL2(
  caseId: string,
  data: {
    rep_id: string;
    answers: { group_id: number; answer_value: string | string[] }[];
    note?: string;
  }
) {
  return apiFetch<any>(`/queue/${caseId}/rerun`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

// Pathway change
export async function updatePathway(
  caseId: string,
  data: { pathway_id: string; pathway_name: string; rep_id: string }
) {
  return apiFetch<any>(`/queue/${caseId}/pathway`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

// Bypass rules
export async function listBypassRules() {
  return apiFetch<any[]>("/settings/bypass-rules");
}

export async function createBypassRule(data: any) {
  return apiFetch<any>("/settings/bypass-rules", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function updateBypassRule(id: string, data: any) {
  return apiFetch<any>(`/settings/bypass-rules/${id}`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

export async function deleteBypassRule(id: string) {
  return apiFetch<any>(`/settings/bypass-rules/${id}`, {
    method: "DELETE",
  });
}

export async function flagCase(caseId: string, rep_id: string, reason: string) {
  return apiFetch(`/queue/${caseId}/flag`, {
    method: "POST",
    body: JSON.stringify({ rep_id, reason }),
  });
}

export async function sendToHold(caseId: string, rep_id: string, reason: string) {
  return apiFetch(`/queue/${caseId}/send-to-hold`, {
    method: "POST",
    body: JSON.stringify({ rep_id, reason }),
  });
}

export async function validateFax(
  caseId: string,
  data: { action: "approved" | "rejected"; rep_id: string; reason?: string }
) {
  return apiFetch<any>(`/queue/${caseId}/validate-fax`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

// Clinical review (signature replay cases)
export async function confirmClinicalReview(
  caseId: string,
  data: { rep_id: string; note?: string }
) {
  return apiFetch<any>(`/queue/${caseId}/confirm-clinical`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function rejectClinicalReview(
  caseId: string,
  data: { rep_id: string; note?: string }
) {
  return apiFetch<any>(`/queue/${caseId}/reject-clinical`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

// No-auth review (portal says DI doesn't require pre-authorization)
export async function confirmNoAuth(
  caseId: string,
  data: { rep_id: string; note?: string }
) {
  return apiFetch<any>(`/queue/${caseId}/confirm-no-auth`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function rejectNoAuth(
  caseId: string,
  data: { rep_id: string; note?: string }
) {
  return apiFetch<any>(`/queue/${caseId}/reject-no-auth`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function markAlreadyWorked(
  caseId: string,
  data: { rep_id: string; note?: string }
) {
  return apiFetch<any>(`/queue/${caseId}/mark-already-worked`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

// Algorithm signatures
export async function listSignatures(limit = 100) {
  return apiFetch<any[]>(`/signatures?limit=${limit}`);
}

export async function getSignatureStats() {
  return apiFetch<any>("/signatures/stats");
}

export async function scanSignatures(limit = 50) {
  return apiFetch<any>(`/signatures/scan?limit=${limit}`, { method: "POST" });
}

export async function uploadNotes(caseId: string, file: File) {
  const formData = new FormData();
  formData.append("file", file);
  const res = await fetch(`${API_BASE}/cases/${caseId}/notes`, {
    method: "POST",
    body: formData,
  });
  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(error.detail || `Upload failed: ${res.status}`);
  }
  return res.json();
}

export async function resolveClinicals(caseId: string) {
  return apiFetch<any>(`/cases/${caseId}/resolve-clinicals`, { method: "POST" });
}

export async function syncFromMongo(params?: { extract?: boolean; limit?: number }) {
  const searchParams = new URLSearchParams();
  if (params?.extract !== undefined) searchParams.set("extract", String(params.extract));
  if (params?.limit) searchParams.set("limit", String(params.limit));
  const qs = searchParams.toString();
  return apiFetch<any>(`/sync${qs ? `?${qs}` : ""}`, { method: "POST" });
}

export async function listBatches() {
  return apiFetch<any[]>("/batches");
}

// ── Submission Jobs Queue ──

export async function getJobStats() {
  return apiFetch<any>("/jobs/stats");
}

export async function listJobs(params?: {
  status?: string;
  is_stat?: boolean;
  exception_type?: string;
  limit?: number;
  offset?: number;
}) {
  const searchParams = new URLSearchParams();
  if (params?.status) searchParams.set("status", params.status);
  if (params?.is_stat !== undefined) searchParams.set("is_stat", String(params.is_stat));
  if (params?.exception_type) searchParams.set("exception_type", params.exception_type);
  if (params?.limit) searchParams.set("limit", String(params.limit));
  if (params?.offset) searchParams.set("offset", String(params.offset));
  const qs = searchParams.toString();
  return apiFetch<any[]>(`/jobs${qs ? `?${qs}` : ""}`);
}

export async function listQueuedJobs(statOnly = false) {
  return apiFetch<any[]>(`/jobs/queued?stat_only=${statOnly}`);
}

export async function listExceptions(exceptionType?: string) {
  const qs = exceptionType ? `?exception_type=${exceptionType}` : "";
  return apiFetch<any[]>(`/jobs/exceptions${qs}`);
}

export async function enqueueJobs(caseIds: string[]) {
  return apiFetch<any>("/jobs/enqueue", {
    method: "POST",
    body: JSON.stringify({ case_ids: caseIds }),
  });
}

export async function resolveException(
  jobId: string,
  data: { rep_id: string; action: string; note?: string; data?: any }
) {
  return apiFetch<any>(`/jobs/${jobId}/resolve`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function listWorkers() {
  return apiFetch<any[]>("/jobs/workers");
}

// ── Worker Account Management ──

export async function listWorkerAccounts() {
  return apiFetch<any[]>("/settings/workers");
}

export async function createWorkerAccount(body: {
  container_id: string;
  username: string;
  password: string;
  worker_url?: string;
  mailbox_address: string;
  shift: string;
  job_type?: string;
}) {
  return apiFetch<any>("/settings/workers", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function updateWorkerAccount(id: string, body: Record<string, any>) {
  return apiFetch<any>(`/settings/workers/${id}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export async function deleteWorkerAccount(id: string) {
  return apiFetch<any>(`/settings/workers/${id}`, { method: "DELETE" });
}

export async function toggleWorkerAccount(id: string) {
  return apiFetch<any>(`/settings/workers/${id}/toggle`, { method: "POST" });
}

// ── Analytics ──

export async function getAnalyticsOverview() {
  return apiFetch<any>("/analytics/overview");
}

export async function getAnalyticsOverrides() {
  return apiFetch<any>("/analytics/overrides");
}

export async function getAnalyticsOutcomes() {
  return apiFetch<any>("/analytics/outcomes");
}

export async function getAnalyticsCoverage() {
  return apiFetch<any>("/analytics/coverage");
}

export async function getAnalyticsApprovalBreakdown() {
  return apiFetch<any>("/analytics/approval-breakdown");
}

export async function getAnalyticsPathwayIntelligence() {
  return apiFetch<any>("/analytics/pathway-intelligence");
}

export async function getAnalyticsSubmissionSignatures() {
  return apiFetch<any>("/analytics/submission-signatures");
}

// ── Executions ──

export async function getExecutionsSummary(days = 7) {
  return apiFetch<any>(`/executions/summary?days=${days}`);
}

export async function getExecutionsRecent(limit = 50, eventType?: string) {
  const params = new URLSearchParams({ limit: String(limit) });
  if (eventType) params.set("event_type", eventType);
  return apiFetch<any[]>(`/executions/recent?${params}`);
}

// ── Settings ──

export async function getSettings() {
  return apiFetch<Record<string, any>>("/settings");
}

export async function updateSetting(key: string, value: any) {
  return apiFetch<any>(`/settings/${key}`, {
    method: "PUT",
    body: JSON.stringify({ value }),
  });
}

export async function flushPreview() {
  return apiFetch<any>("/settings/flush/preview");
}

export async function flushCases(force: boolean = false) {
  return apiFetch<any>(`/settings/flush?force=${force}`, { method: "POST" });
}

export async function syncNow() {
  return apiFetch<any>("/settings/sync-now", { method: "POST" });
}

export async function getSyncHistory() {
  return apiFetch<any[]>("/settings/sync-history");
}

export async function resetPrompt(promptKey: string) {
  return apiFetch<any>(`/settings/prompt-reset/${promptKey}`, { method: "POST" });
}

// Portal processing controls
export async function extractNow() {
  return apiFetch<any>("/settings/extract-now", { method: "POST" });
}

export async function submitBatch() {
  return apiFetch<any>("/settings/submit-batch", { method: "POST" });
}

export async function startWorker() {
  return apiFetch<any>("/settings/start-worker", { method: "POST" });
}

// RingCentral Integration
export async function getRcSettings() {
  return apiFetch<Record<string, any>>("/settings/integrations/rc");
}

export async function updateRcSettings(data: Record<string, any>) {
  return apiFetch<any>("/settings/integrations/rc", {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

export async function testRcConnection() {
  return apiFetch<{ ok: boolean; message?: string; error?: string }>("/settings/integrations/rc/test", {
    method: "POST",
  });
}

// Availity Integration
export async function listAvailityJobs(params?: {
  status?: string;
  page?: number;
  page_size?: number;
}) {
  const qs = new URLSearchParams();
  if (params?.status) qs.set("status", params.status);
  if (params?.page) qs.set("page", String(params.page));
  if (params?.page_size) qs.set("page_size", String(params.page_size));
  const suffix = qs.toString() ? `?${qs}` : "";
  return apiFetch<any>(`/availity/jobs${suffix}`);
}

export async function getAvailityJob(jobId: string) {
  return apiFetch<any>(`/availity/jobs/${jobId}`);
}

export async function deleteAvailityJob(jobId: string) {
  return apiFetch<any>(`/availity/jobs/${jobId}`, { method: "DELETE" });
}

export async function sendMemberNotFoundToAvaility() {
  return apiFetch<any>(`/availity/send-member-not-found`, { method: "POST" });
}

export async function getMemberNotFoundCount() {
  return apiFetch<{ total: number; ready_to_send: number; skipped_incomplete: number }>(
    `/availity/member-not-found-count`
  );
}

export async function getAvailitySettings() {
  return apiFetch<any>(`/availity/settings`);
}

export async function updateAvailitySettings(data: Record<string, any>) {
  return apiFetch<any>(`/availity/settings`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

export async function testAvailityConnection() {
  return apiFetch<{ ok: boolean; message?: string; error?: string }>(
    `/availity/test-connection`,
    { method: "POST" }
  );
}

// Authentication
export async function login(username: string, password: string) {
  const res = await fetch(`${API_BASE}/auth/proxy-login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ username, password }),
  });
  const data = await res.json();
  if (!res.ok || !data.success) {
    throw new Error(data.error || "Login failed");
  }
  return data;
}

export async function getMe() {
  const res = await fetch(`${API_BASE}/auth/me`, {
    credentials: "include",
  });
  if (!res.ok) return null;
  const data = await res.json();
  return data.authenticated ? data : null;
}

export async function logout() {
  await fetch(`${API_BASE}/auth/proxy-logout`, {
    method: "POST",
    credentials: "include",
  });
}
