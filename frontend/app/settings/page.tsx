"use client";

import { useEffect, useState } from "react";
import {
  getSettings,
  updateSetting,
  flushPreview,
  flushCases,
  syncNow,
  getSyncHistory,
  resetPrompt,
  getRcSettings,
  updateRcSettings,
  testRcConnection,
  getAvailitySettings,
  updateAvailitySettings,
  testAvailityConnection,
  getSignatureStats,
  scanSignatures,
} from "@/lib/api";

type Tab = "General" | "Workers" | "Automation" | "Integrations";

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState<Tab>("General");
  const [settings, setSettings] = useState<Record<string, any>>({});
  const [history, setHistory] = useState<any[]>([]);
  const [bypassRules, setBypassRules] = useState<any[]>([]);
  const [workerAccounts, setWorkerAccounts] = useState<any[]>([]);
  const [flushInfo, setFlushInfo] = useState<any>(null);
  const [activePrompt, setActivePrompt] = useState("");
  const [promptDraft, setPromptDraft] = useState("");
  const [apiKeyDrafts, setApiKeyDrafts] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [flushing, setFlushing] = useState(false);
  const [showFlushConfirm, setShowFlushConfirm] = useState(false);
  const [message, setMessage] = useState<{ text: string; type: "success" | "error" } | null>(null);
  const [editingWorkerId, setEditingWorkerId] = useState<string | null>(null);
  const [workerEditData, setWorkerEditData] = useState<Record<string, any>>({});

  // RingCentral state
  const [rcSettings, setRcSettings] = useState<Record<string, any>>({
    fax_enabled: false,
    client_id: "",
    client_secret: "",
    jwt_token: "",
    server_url: "https://platform.ringcentral.com",
    carelon_fax_number: "+18007982068",
  });
  const [rcTesting, setRcTesting] = useState(false);
  const [rcSaving, setRcSaving] = useState(false);

  // Availity state
  const [availitySettings, setAvailitySettings] = useState<Record<string, any>>({
    availity_base_url: "https://api-v2.ronexa.com",
    availity_username: "",
    availity_password: "",
    availity_auto_send_enabled: false,
    availity_auto_send_interval_minutes: 30,
  });
  const [availityTesting, setAvailityTesting] = useState(false);
  const [availitySaving, setAvailitySaving] = useState(false);

  // Awaiting Clinicals / Signature Replay state
  const [sigStats, setSigStats] = useState<any>(null);
  const [sigScanning, setSigScanning] = useState(false);
  const [sigScanResult, setSigScanResult] = useState<any>(null);

  // Open prompt editor — fetches default from backend if no value in settings
  const openPromptEditor = async (key: string) => {
    setActivePrompt(key);
    if (settings[key]) {
      setPromptDraft(settings[key]);
    } else {
      // No value in settings — fetch default template from backend
      try {
        const result = await resetPrompt(key);
        const defaultText = result.value || result.default || "";
        setPromptDraft(defaultText);
        setSettings((prev: any) => ({ ...prev, [key]: defaultText }));
      } catch {
        setPromptDraft("(Failed to load default template)");
      }
    }
  };

  const refresh = async () => {
    try {
      const { listBypassRules, listWorkerAccounts } = await import("@/lib/api");
      const [s, h, rules, workers, rc, av] = await Promise.all([
        getSettings(),
        getSyncHistory(),
        listBypassRules().catch(() => []),
        listWorkerAccounts().catch(() => []),
        getRcSettings().catch(() => null),
        getAvailitySettings().catch(() => null),
      ]);
      setSettings(s);
      setHistory(h);
      setBypassRules(rules);
      setWorkerAccounts(workers);
      if (rc) {
        setRcSettings((prev) => ({ ...prev, ...rc }));
      }
      if (av) {
        setAvailitySettings((prev) => ({ ...prev, ...av }));
      }
    } catch {
      // API not running
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
    getSignatureStats().then(setSigStats).catch(console.error);
  }, []);

  const handleUpdate = async (key: string, value: any) => {
    try {
      await updateSetting(key, value);
      setSettings((prev) => ({ ...prev, [key]: value }));
      showMessage(`Updated ${key}`, "success");
    } catch (err: any) {
      showMessage(err.message, "error");
    }
  };

  const handleSyncNow = async () => {
    setSyncing(true);
    setMessage(null);
    try {
      const result = await syncNow();
      showMessage(
        `Synced: ${result.new_cases} new, ${result.duplicates_skipped} dupes, ${result.enqueued || 0} enqueued`,
        "success"
      );
      refresh();
    } catch (err: any) {
      showMessage(`Sync failed: ${err.message}`, "error");
    } finally {
      setSyncing(false);
    }
  };

  const handleSigScan = async () => {
    setSigScanning(true);
    setSigScanResult(null);
    try {
      const result = await scanSignatures(50);
      setSigScanResult(result);
      const stats = await getSignatureStats();
      setSigStats(stats);
    } catch (err: any) {
      showMessage(`Signature scan failed: ${err.message}`, "error");
    } finally {
      setSigScanning(false);
    }
  };

  const handleFlushPreview = async () => {
    try {
      const info = await flushPreview();
      setFlushInfo(info);
      setShowFlushConfirm(true);
    } catch (err: any) {
      showMessage(`Preview failed: ${err.message}`, "error");
    }
  };

  const handleFlush = async (force: boolean = false) => {
    setFlushing(true);
    try {
      const result = await flushCases(force);
      showMessage(
        force
          ? `Force flushed ALL ${result.flushed} cases (including review)`
          : `Flushed ${result.flushed} cases (${result.protected} protected)`,
        "success"
      );
      setShowFlushConfirm(false);
      setFlushInfo(null);
      refresh();
    } catch (err: any) {
      showMessage(`Flush failed: ${err.message}`, "error");
    } finally {
      setFlushing(false);
    }
  };

  const handleTestRcConnection = async () => {
    setRcTesting(true);
    try {
      const result = await testRcConnection();
      if (result.ok) {
        showMessage(result.message || "RingCentral connection successful", "success");
      } else {
        showMessage(result.error || "RingCentral connection failed", "error");
      }
    } catch (err: any) {
      showMessage(`Connection test failed: ${err.message}`, "error");
    } finally {
      setRcTesting(false);
    }
  };

  const handleSaveRcSettings = async () => {
    setRcSaving(true);
    try {
      await updateRcSettings(rcSettings);
      showMessage("RingCentral settings saved", "success");
    } catch (err: any) {
      showMessage(`Save failed: ${err.message}`, "error");
    } finally {
      setRcSaving(false);
    }
  };

  const handleTestAvailityConnection = async () => {
    setAvailityTesting(true);
    try {
      const result = await testAvailityConnection();
      if (result.ok) {
        showMessage(result.message || "Availity connection successful", "success");
      } else {
        showMessage(result.error || "Availity connection failed", "error");
      }
    } catch (err: any) {
      showMessage(`Connection test failed: ${err.message}`, "error");
    } finally {
      setAvailityTesting(false);
    }
  };

  const handleSaveAvailitySettings = async () => {
    setAvailitySaving(true);
    try {
      await updateAvailitySettings(availitySettings);
      showMessage("Availity settings saved", "success");
    } catch (err: any) {
      showMessage(`Save failed: ${err.message}`, "error");
    } finally {
      setAvailitySaving(false);
    }
  };

  const showMessage = (text: string, type: "success" | "error") => {
    setMessage({ text, type });
    setTimeout(() => setMessage(null), 5000);
  };

  if (loading) {
    return <p className="text-sm text-gray-400 py-8 text-center">Loading settings...</p>;
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Settings</h1>

      {message && (
        <div
          className={`p-3 rounded text-sm ${
            message.type === "success"
              ? "bg-green-50 text-green-700 border border-green-200"
              : "bg-red-50 text-red-700 border border-red-200"
          }`}
        >
          {message.text}
        </div>
      )}

      {/* Tab Bar */}
      <div className="flex border-b mb-6">
        {(["General", "Workers", "Automation", "Integrations"] as Tab[]).map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`px-6 py-3 text-sm font-medium border-b-2 transition-colors ${
              activeTab === tab
                ? "border-blue-600 text-blue-600"
                : "border-transparent text-gray-500 hover:text-gray-700"
            }`}
          >
            {tab}
          </button>
        ))}
      </div>

      {/* ═══════════════════════════════════════════════════ */}
      {/* GENERAL TAB                                        */}
      {/* ═══════════════════════════════════════════════════ */}
      {activeTab === "General" && (
        <div className="space-y-6">
          {/* Review Configuration */}
          <div className="border rounded-lg p-5 space-y-4">
            <h2 className="font-semibold text-lg">Review Configuration</h2>
            <p className="text-xs text-gray-400">Control the two-level review flow. Bypass rules auto-approve known CPT+ICD combinations.</p>

            <div className="grid grid-cols-4 gap-4">
              <div className="flex items-center justify-between border rounded p-3">
                <div>
                  <p className="text-sm font-medium">Level 1 Review</p>
                  <p className="text-xs text-gray-400">When OFF, cases skip L1</p>
                </div>
                <button
                  onClick={() => handleUpdate("l1_review_enabled", !settings.l1_review_enabled)}
                  className={`px-3 py-1 rounded text-sm font-medium transition ${
                    settings.l1_review_enabled !== false
                      ? "bg-green-100 text-green-700"
                      : "bg-gray-100 text-gray-500"
                  }`}
                >
                  {settings.l1_review_enabled !== false ? "ON" : "OFF"}
                </button>
              </div>

              <div className="flex items-center justify-between border rounded p-3">
                <div>
                  <p className="text-sm font-medium">Level 2 Review</p>
                  <p className="text-xs text-gray-400">When OFF, L1 triggers submit</p>
                </div>
                <button
                  onClick={() => handleUpdate("l2_review_enabled", !settings.l2_review_enabled)}
                  className={`px-3 py-1 rounded text-sm font-medium transition ${
                    settings.l2_review_enabled !== false
                      ? "bg-green-100 text-green-700"
                      : "bg-gray-100 text-gray-500"
                  }`}
                >
                  {settings.l2_review_enabled !== false ? "ON" : "OFF"}
                </button>
              </div>

              <div className="flex items-center justify-between border rounded p-3">
                <div>
                  <p className="text-sm font-medium">Auto-Bypass</p>
                  <p className="text-xs text-gray-400">Skip review for known combos</p>
                </div>
                <button
                  onClick={() => handleUpdate("bypass_enabled", !settings.bypass_enabled)}
                  className={`px-3 py-1 rounded text-sm font-medium transition ${
                    settings.bypass_enabled
                      ? "bg-green-100 text-green-700"
                      : "bg-gray-100 text-gray-500"
                  }`}
                >
                  {settings.bypass_enabled ? "ON" : "OFF"}
                </button>
              </div>

              <div className="flex items-center justify-between border rounded p-3">
                <div>
                  <p className="text-sm font-medium">Fax Validation</p>
                  <p className="text-xs text-gray-400">Review fax before sending</p>
                </div>
                <button
                  onClick={() => handleUpdate("fax_validation_enabled", !settings.fax_validation_enabled)}
                  className={`px-3 py-1 rounded text-sm font-medium transition ${
                    settings.fax_validation_enabled !== false
                      ? "bg-green-100 text-green-700"
                      : "bg-gray-100 text-gray-500"
                  }`}
                >
                  {settings.fax_validation_enabled !== false ? "ON" : "OFF"}
                </button>
              </div>
            </div>

            {/* Bypass Rules Table */}
            {settings.bypass_enabled && (
              <div className="border-t pt-4 space-y-3">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-medium">Bypass Rules</h3>
                  <button
                    onClick={async () => {
                      const { createBypassRule } = await import("@/lib/api");
                      await createBypassRule({
                        cpt_code: "",
                        icd_code: "",
                        min_confidence: 85,
                        bypass_l1: true,
                        bypass_l2: false,
                        enabled: false,
                      });
                      refresh();
                    }}
                    className="px-3 py-1 bg-blue-600 text-white rounded text-xs hover:bg-blue-700"
                  >
                    + Add Rule
                  </button>
                </div>
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs text-gray-500 border-b">
                      <th className="pb-2">CPT</th>
                      <th className="pb-2">ICD</th>
                      <th className="pb-2">Min Confidence</th>
                      <th className="pb-2">Skip L1</th>
                      <th className="pb-2">Skip L2</th>
                      <th className="pb-2">Approved</th>
                      <th className="pb-2">Enabled</th>
                      <th className="pb-2"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {(bypassRules || []).map((rule: any) => (
                      <tr key={rule.id} className="border-b">
                        <td className="py-2">
                          <input
                            type="text"
                            defaultValue={rule.cpt_code}
                            onBlur={async (e) => {
                              const { updateBypassRule } = await import("@/lib/api");
                              await updateBypassRule(rule.id, { cpt_code: e.target.value });
                            }}
                            className="border rounded px-1 py-0.5 w-20 text-xs font-mono"
                            placeholder="70551"
                          />
                        </td>
                        <td className="py-2">
                          <input
                            type="text"
                            defaultValue={rule.icd_code}
                            onBlur={async (e) => {
                              const { updateBypassRule } = await import("@/lib/api");
                              await updateBypassRule(rule.id, { icd_code: e.target.value });
                            }}
                            className="border rounded px-1 py-0.5 w-20 text-xs font-mono"
                            placeholder="M54.5"
                          />
                        </td>
                        <td className="py-2">
                          <input
                            type="number"
                            defaultValue={rule.min_confidence}
                            onBlur={async (e) => {
                              const { updateBypassRule } = await import("@/lib/api");
                              await updateBypassRule(rule.id, { min_confidence: parseFloat(e.target.value) });
                            }}
                            className="border rounded px-1 py-0.5 w-14 text-xs font-mono"
                          />
                        </td>
                        <td className="py-2 text-center">
                          <input
                            type="checkbox"
                            checked={rule.bypass_l1}
                            onChange={async (e) => {
                              const { updateBypassRule } = await import("@/lib/api");
                              await updateBypassRule(rule.id, { bypass_l1: e.target.checked });
                              refresh();
                            }}
                          />
                        </td>
                        <td className="py-2 text-center">
                          <input
                            type="checkbox"
                            checked={rule.bypass_l2}
                            onChange={async (e) => {
                              const { updateBypassRule } = await import("@/lib/api");
                              await updateBypassRule(rule.id, { bypass_l2: e.target.checked });
                              refresh();
                            }}
                          />
                        </td>
                        <td className="py-2 font-mono text-xs text-center">{rule.approved_count}</td>
                        <td className="py-2 text-center">
                          <input
                            type="checkbox"
                            checked={rule.enabled}
                            onChange={async (e) => {
                              const { updateBypassRule } = await import("@/lib/api");
                              await updateBypassRule(rule.id, { enabled: e.target.checked });
                              refresh();
                            }}
                          />
                        </td>
                        <td className="py-2">
                          <button
                            onClick={async () => {
                              const { deleteBypassRule } = await import("@/lib/api");
                              await deleteBypassRule(rule.id);
                              refresh();
                            }}
                            className="text-red-500 hover:text-red-700 text-xs"
                          >
                            Delete
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* Polling / Sync Settings */}
          <div className="grid grid-cols-2 gap-6">
            {/* Mongo Polling */}
            <div className="border rounded-lg p-5 space-y-4">
              <h2 className="font-semibold text-lg">Mongo Polling</h2>

              <div className="flex items-center justify-between">
                <label className="text-sm">Polling Enabled</label>
                <button
                  onClick={() => handleUpdate("polling_enabled", !settings.polling_enabled)}
                  className={`px-3 py-1 rounded text-sm font-medium transition ${
                    settings.polling_enabled
                      ? "bg-green-100 text-green-700"
                      : "bg-gray-100 text-gray-500"
                  }`}
                >
                  {settings.polling_enabled ? "ON" : "OFF"}
                </button>
              </div>

              <div className="flex items-center justify-between">
                <label className="text-sm">Interval (minutes)</label>
                <input
                  type="number"
                  min={1}
                  max={120}
                  value={settings.polling_interval_minutes || 15}
                  onChange={(e) =>
                    handleUpdate("polling_interval_minutes", parseInt(e.target.value) || 15)
                  }
                  className="border rounded px-2 py-1 w-20 text-sm text-right"
                />
              </div>

              <div className="flex items-center justify-between">
                <label className="text-sm">Extract PDFs (Vision)</label>
                <button
                  onClick={() => handleUpdate("polling_extract", !settings.polling_extract)}
                  className={`px-3 py-1 rounded text-sm font-medium transition ${
                    settings.polling_extract
                      ? "bg-green-100 text-green-700"
                      : "bg-gray-100 text-gray-500"
                  }`}
                >
                  {settings.polling_extract ? "ON" : "OFF"}
                </button>
              </div>

              <div className="flex items-center justify-between">
                <label className="text-sm">Limit per sync</label>
                <input
                  type="number"
                  min={1}
                  max={1000}
                  value={settings.polling_limit || 500}
                  onChange={(e) =>
                    handleUpdate("polling_limit", parseInt(e.target.value) || 500)
                  }
                  className="border rounded px-2 py-1 w-20 text-sm text-right"
                />
              </div>

              {/* Last sync info */}
              {settings.last_sync_at && (
                <div className="text-xs text-gray-500 border-t pt-3">
                  <p>Last sync: {formatTimestamp(settings.last_sync_at)}</p>
                  {settings.last_sync_result && (
                    <p>
                      {settings.last_sync_result.new_cases || 0} new,{" "}
                      {settings.last_sync_result.enqueued || 0} enqueued
                    </p>
                  )}
                </div>
              )}

              <button
                onClick={handleSyncNow}
                disabled={syncing}
                className="w-full px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50"
              >
                {syncing ? "Syncing..." : "Sync Now"}
              </button>
            </div>

            {/* Daily Flush */}
            <div className="border rounded-lg p-5 space-y-4">
              <h2 className="font-semibold text-lg">Database Flush</h2>

              <div className="flex items-center justify-between">
                <label className="text-sm">Auto-flush Enabled</label>
                <button
                  onClick={() => handleUpdate("flush_enabled", !settings.flush_enabled)}
                  className={`px-3 py-1 rounded text-sm font-medium transition ${
                    settings.flush_enabled
                      ? "bg-green-100 text-green-700"
                      : "bg-gray-100 text-gray-500"
                  }`}
                >
                  {settings.flush_enabled ? "ON" : "OFF"}
                </button>
              </div>

              <div className="flex items-center justify-between">
                <label className="text-sm">Flush Time</label>
                <input
                  type="time"
                  value={settings.flush_time || "04:00"}
                  onChange={(e) => handleUpdate("flush_time", e.target.value)}
                  className="border rounded px-2 py-1 text-sm"
                />
              </div>

              {/* Protected states */}
              <div className="bg-gray-50 rounded p-3">
                <p className="text-xs font-medium text-gray-600 mb-2">Protected States (skipped by normal flush, use "Flush ALL" to include):</p>
                <div className="flex gap-2">
                  {["L1_REVIEW", "L2_REVIEW", "PROCESSING", "SUBMITTING"].map((s) => (
                    <span
                      key={s}
                      className="px-2 py-0.5 bg-white border rounded text-xs text-gray-600"
                    >
                      {s}
                    </span>
                  ))}
                </div>
              </div>

              {/* Last flush info */}
              {settings.last_flush_at && (
                <div className="text-xs text-gray-500 border-t pt-3">
                  <p>Last flush: {formatTimestamp(settings.last_flush_at)}</p>
                  {settings.last_flush_result && (
                    <p>{settings.last_flush_result.flushed} cases removed</p>
                  )}
                </div>
              )}

              <button
                onClick={handleFlushPreview}
                className="w-full px-4 py-2 bg-red-600 text-white rounded text-sm hover:bg-red-700"
              >
                Flush Now
              </button>
            </div>
          </div>

          {/* Flush Confirmation Dialog */}
          {showFlushConfirm && flushInfo && (
            <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
              <div className="bg-white rounded-lg p-6 max-w-md w-full shadow-xl space-y-4">
                <h3 className="font-semibold text-lg">Confirm Database Flush</h3>

                <div className="bg-red-50 border border-red-200 rounded p-3">
                  <p className="text-sm text-red-700 font-medium">
                    This will permanently delete {flushInfo.total} cases.
                  </p>
                  <p className="text-xs text-red-600 mt-1">
                    {flushInfo.protected} cases are protected and will NOT be deleted.
                  </p>
                </div>

                {flushInfo.by_state && (
                  <div className="text-xs space-y-1">
                    {Object.entries(flushInfo.by_state).map(([state, count]) => (
                      <div key={state} className="flex justify-between">
                        <span className="text-gray-600">{state}</span>
                        <span className="font-mono">{count as number}</span>
                      </div>
                    ))}
                  </div>
                )}

                <div className="flex gap-2 pt-2">
                  {flushInfo.total > 0 && (
                    <button
                      onClick={() => handleFlush(false)}
                      disabled={flushing}
                      className="flex-1 px-4 py-2 bg-red-600 text-white rounded text-sm hover:bg-red-700 disabled:opacity-50"
                    >
                      {flushing ? "Flushing..." : `Delete ${flushInfo.total} Cases`}
                    </button>
                  )}
                  {flushInfo.protected > 0 && (
                    <button
                      onClick={() => handleFlush(true)}
                      disabled={flushing}
                      className="flex-1 px-4 py-2 bg-red-900 text-white rounded text-sm hover:bg-red-950 disabled:opacity-50"
                    >
                      {flushing ? "Flushing..." : "Flush ALL (incl. review)"}
                    </button>
                  )}
                  <button
                    onClick={() => {
                      setShowFlushConfirm(false);
                      setFlushInfo(null);
                    }}
                    className="flex-1 px-4 py-2 bg-gray-200 text-gray-700 rounded text-sm hover:bg-gray-300"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* Portal Processing */}
          <div className="border rounded-lg p-5 space-y-4">
            <h2 className="font-semibold text-lg">Portal Processing</h2>
            <p className="text-xs text-gray-400">Control extraction, portal automation, and batch submission.</p>

            <div className="grid grid-cols-4 gap-4">
              <div className="flex items-center justify-between border rounded p-3">
                <div>
                  <p className="text-sm font-medium">Auto-Process</p>
                  <p className="text-xs text-gray-400">Run portal after extraction</p>
                </div>
                <button
                  onClick={() => handleUpdate("auto_process_enabled", !settings.auto_process_enabled)}
                  className={`px-3 py-1 rounded text-sm font-medium transition ${
                    settings.auto_process_enabled
                      ? "bg-green-100 text-green-700"
                      : "bg-gray-100 text-gray-500"
                  }`}
                >
                  {settings.auto_process_enabled ? "ON" : "OFF"}
                </button>
              </div>

              <div className="flex items-center justify-between border rounded p-3">
                <div>
                  <p className="text-sm font-medium">Extraction Batch</p>
                  <p className="text-xs text-gray-400">Concurrent OCR extractions</p>
                </div>
                <input
                  type="number"
                  min={1}
                  max={50}
                  value={settings.extraction_batch_size || 10}
                  onChange={(e) => handleUpdate("extraction_batch_size", parseInt(e.target.value) || 10)}
                  className="border rounded px-2 py-1 w-16 text-sm text-right"
                />
              </div>

              <div className="flex items-center justify-between border rounded p-3">
                <div>
                  <p className="text-sm font-medium">Portal Batch</p>
                  <p className="text-xs text-gray-400">Cases per worker loop</p>
                </div>
                <input
                  type="number"
                  min={1}
                  max={200}
                  value={settings.portal_batch_size || 25}
                  onChange={(e) => handleUpdate("portal_batch_size", parseInt(e.target.value) || 25)}
                  className="border rounded px-2 py-1 w-16 text-sm text-right"
                />
              </div>

              <div className="flex items-center justify-between border rounded p-3">
                <div>
                  <p className="text-sm font-medium">Finalize Batch</p>
                  <p className="text-xs text-gray-400">Approved per submit run</p>
                </div>
                <input
                  type="number"
                  min={1}
                  max={50}
                  value={settings.finalize_batch_size || 10}
                  onChange={(e) => handleUpdate("finalize_batch_size", parseInt(e.target.value) || 10)}
                  className="border rounded px-2 py-1 w-16 text-sm text-right"
                />
              </div>
            </div>

            <div className="flex gap-2">
              <button
                onClick={async () => {
                  if (!confirm("Run OCR extraction on all pending cases?")) return;
                  setMessage({ text: "Extracting...", type: "success" });
                  try {
                    const { extractNow } = await import("@/lib/api");
                    const result = await extractNow();
                    showMessage(result.message || `Extraction triggered: ${result.count} cases`, "success");
                  } catch (err: any) {
                    showMessage(err.message, "error");
                  }
                }}
                className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700"
              >
                Extract Now
              </button>
              <button
                onClick={async () => {
                  if (!confirm("Start portal batch processing? This will login to Carelon and process queued cases.")) return;
                  setMessage({ text: "Starting worker...", type: "success" });
                  try {
                    const { startWorker } = await import("@/lib/api");
                    const result = await startWorker();
                    showMessage(result.message || "Worker started", "success");
                  } catch (err: any) {
                    showMessage(err.message, "error");
                  }
                }}
                className="px-4 py-2 bg-purple-600 text-white rounded text-sm hover:bg-purple-700"
              >
                Start Portal Batch
              </button>
              <button
                onClick={async () => {
                  if (!confirm("Submit all approved cases to the portal? This will resolve awakeables and finalize submissions.")) return;
                  setMessage({ text: "Submitting batch...", type: "success" });
                  try {
                    const { submitBatch } = await import("@/lib/api");
                    const result = await submitBatch();
                    showMessage(result.message || `Submit triggered: ${result.count} cases`, "success");
                  } catch (err: any) {
                    showMessage(err.message, "error");
                  }
                }}
                className="px-4 py-2 bg-green-600 text-white rounded text-sm hover:bg-green-700"
              >
                Submit Batch
              </button>
            </div>
          </div>

          {/* Sync History */}
          <div className="border rounded-lg p-5">
            <h2 className="font-semibold text-lg mb-3">Sync History</h2>
            {history.length === 0 ? (
              <p className="text-sm text-gray-400">No sync runs yet</p>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-gray-500 border-b">
                    <th className="pb-2 font-medium">Timestamp</th>
                    <th className="pb-2 font-medium">Fetched</th>
                    <th className="pb-2 font-medium">New</th>
                    <th className="pb-2 font-medium">Dupes</th>
                    <th className="pb-2 font-medium">Extracted</th>
                    <th className="pb-2 font-medium">Enqueued</th>
                    <th className="pb-2 font-medium">Errors</th>
                  </tr>
                </thead>
                <tbody>
                  {history.map((h: any, i: number) => (
                    <tr key={i} className="border-b">
                      <td className="py-1.5 text-xs text-gray-500">
                        {formatTimestamp(h.timestamp)}
                      </td>
                      <td className="py-1.5 font-mono">{h.total_fetched}</td>
                      <td className="py-1.5 font-mono text-green-600">{h.new_cases}</td>
                      <td className="py-1.5 font-mono text-gray-400">{h.duplicates_skipped}</td>
                      <td className="py-1.5 font-mono">{h.notes_extracted}</td>
                      <td className="py-1.5 font-mono text-blue-600">{h.enqueued}</td>
                      <td className="py-1.5 font-mono text-red-500">{h.errors || 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {/* LLM Pipeline — Workflow Nodes (System Prompt) */}
          <div className="border rounded-lg p-5 space-y-4">
            <h2 className="font-semibold text-lg">AI Pipeline</h2>
            <p className="text-xs text-gray-400">Each node represents a workflow step. Settings saved to database in real-time.</p>

            {/* Row 1: First Pass Pipeline */}
            <div>
              <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">First Pass</p>
              <div className="flex items-start gap-2">
                {/* Node 1: PDF Upload (static) */}
                <div className="border rounded-lg p-4 w-40 bg-gray-50 flex-shrink-0">
                  <div className="flex items-center gap-2 mb-2">
                    <span className="w-2 h-2 rounded-full bg-green-500" />
                    <h3 className="text-sm font-semibold">PDF Upload</h3>
                  </div>
                  <p className="text-xs text-gray-400">Clinical docs from RIS / manual upload</p>
                </div>

                <span className="text-gray-300 self-center text-xl flex-shrink-0">&rarr;</span>

                {/* Node 2: Azure Document Intelligence (OCR — no LLM) */}
                <div className="border-2 border-sky-200 rounded-lg p-4 flex-1 min-w-[170px] bg-sky-50/30">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="w-2 h-2 rounded-full bg-green-500" />
                    <h3 className="text-sm font-semibold">Document OCR</h3>
                  </div>
                  <p className="text-xs text-gray-400 mb-3">Azure AI Document Intelligence</p>
                  <div className="space-y-1 text-xs text-gray-500">
                    <p>Model: <span className="font-mono">prebuilt-read</span></p>
                    <p>Output: clean text + tables</p>
                  </div>
                </div>

                <span className="text-gray-300 self-center text-xl flex-shrink-0">&rarr;</span>

                {/* Node 3: Pathway Q&A (First Pass LLM) */}
                <WorkflowNode
                  title="First Pass LLM"
                  subtitle="Answers Carelon questions from OCR text + RAG"
                  providerKey="llm_eval_provider"
                  modelKey="llm_eval_model"
                  role="eval"
                  promptKeys={["prompt_eval_system", "prompt_eval_user"]}
                  settings={settings}
                  apiKeyDrafts={apiKeyDrafts}
                  onUpdate={handleUpdate}
                  onEditPrompt={openPromptEditor}
                />

                <span className="text-gray-300 self-center text-xl flex-shrink-0">&rarr;</span>

                {/* Node 4: RAG Embeddings */}
                <div className="border-2 border-purple-200 rounded-lg p-4 flex-1 min-w-[150px] bg-purple-50/30">
                  <div className="flex items-center gap-2 mb-2">
                    <span className={`w-2 h-2 rounded-full ${settings.api_key_openai ? "bg-green-500" : "bg-red-400"}`} />
                    <h3 className="text-sm font-semibold">RAG Embeddings</h3>
                  </div>
                  <p className="text-xs text-gray-400 mb-3">Outcome patterns for similarity search</p>
                  <div className="space-y-2">
                    <label className="text-xs text-gray-500">Embedding Model</label>
                    <select
                      value={settings.llm_embed_model || "text-embedding-3-small"}
                      onChange={(e) => handleUpdate("llm_embed_model", e.target.value)}
                      className="w-full border rounded px-2 py-1 text-xs font-mono"
                    >
                      <option value="text-embedding-3-small">text-embedding-3-small</option>
                      <option value="text-embedding-3-large">text-embedding-3-large</option>
                      <option value="text-embedding-ada-002">text-embedding-ada-002</option>
                    </select>
                  </div>
                </div>
              </div>
            </div>

            {/* Row 2: Submission Pipeline */}
            <div>
              <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Submission</p>
              <div className="flex items-start gap-2">
                {/* Submission LLM Node */}
                <WorkflowNode
                  title="Submission LLM"
                  subtitle="Matches portal questions to saved answers during submission"
                  providerKey="llm_match_provider"
                  modelKey="llm_match_model"
                  role="match"
                  promptKeys={["prompt_match_system", "prompt_match_user"]}
                  settings={settings}
                  apiKeyDrafts={apiKeyDrafts}
                  onUpdate={handleUpdate}
                  onEditPrompt={openPromptEditor}
                />

                <span className="text-gray-300 self-center text-xl flex-shrink-0">&rarr;</span>

                {/* Submission outcome (static) */}
                <div className="border rounded-lg p-4 flex-1 min-w-[170px] bg-gray-50">
                  <div className="flex items-center gap-2 mb-2">
                    <span className="w-2 h-2 rounded-full bg-green-500" />
                    <h3 className="text-sm font-semibold">Portal Submit</h3>
                  </div>
                  <p className="text-xs text-gray-400">Steps through questions, matches saved answers, submits to portal</p>
                  <div className="space-y-1 text-xs text-gray-500 mt-2">
                    <p>Match confidence threshold: <span className="font-mono">70%</span></p>
                    <p>Unmatched question: <span className="font-mono text-red-500">HOLD</span></p>
                  </div>
                </div>
              </div>
            </div>

            {/* Row 3: Order-Only Pipeline */}
            <div>
              <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Order Evaluation</p>
              <div className="flex items-start gap-2">
                {/* Order LLM Node */}
                <WorkflowNode
                  title="Order LLM"
                  subtitle="Answers Carelon questions from physician order form + signatures"
                  providerKey="llm_eval_provider"
                  modelKey="llm_eval_model"
                  role="eval"
                  promptKeys={["prompt_eval_order_system", "prompt_eval_order_user"]}
                  settings={settings}
                  apiKeyDrafts={apiKeyDrafts}
                  onUpdate={handleUpdate}
                  onEditPrompt={openPromptEditor}
                />

                <span className="text-gray-300 self-center text-xl flex-shrink-0">&rarr;</span>

                {/* Signature DB stats (info only) */}
                <div className="border-2 border-emerald-200 rounded-lg p-4 flex-1 min-w-[170px] bg-emerald-50/30">
                  <div className="flex items-center gap-2 mb-1">
                    <span className={`w-2 h-2 rounded-full ${sigStats?.total_signatures ? "bg-green-500" : "bg-gray-300"}`} />
                    <h3 className="text-sm font-semibold">Signature DB</h3>
                  </div>
                  <p className="text-xs text-gray-400 mb-3">Proven Q&A patterns injected as LLM context</p>
                  <div className="space-y-1 text-xs text-gray-500">
                    <p>Signatures: <span className="font-mono">{sigStats?.total_signatures ?? "..."}</span></p>
                    <p>CPT+ICD combos: <span className="font-mono">{sigStats?.unique_cpt_icd_combos ?? "..."}</span></p>
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* Prompt Editor — hidden until a prompt button is clicked */}
          {activePrompt && (
            <div className="border rounded-lg p-5 space-y-4">
              <div className="flex items-center justify-between">
                <div>
                  <h2 className="font-semibold text-lg">
                    Editing: {activePrompt.replace("prompt_", "").replace("_", " ").replace(/\b\w/g, c => c.toUpperCase())}
                  </h2>
                  <p className="text-xs text-gray-400">
                    {activePrompt.includes("system") ? "System prompt — static text sent to LLM" :
                     activePrompt === "prompt_eval_user" ? "Jinja2 template — variables: {{ cpt_code }}, {{ icd1 }}, {{ carrier_id }}, {{ question_type }}, {{ question_text }}, {{ options }}, {{ rag_section }}, {{ clinical_section }}, {{ signature_section }}, {{ multi_select }}" :
                     activePrompt === "prompt_eval_order_user" ? "Jinja2 template — variables: same as First Pass (order form context instead of clinical notes, + {{ signature_section }})" :
                     activePrompt === "prompt_match_user" ? "Jinja2 template — variables: {{ question_id }}, {{ question_text }}, {{ question_type }}, {{ options }}, {{ saved_answers }}" :
                     "Jinja2 template — variables: {{ page_count }}, {{ context_str }}, {{ cpt_code }}, {{ cpt_desc }}, {{ icd1 }}, {{ extraction_schema }}"}
                  </p>
                </div>
                <button
                  onClick={() => { setActivePrompt(""); setPromptDraft(""); }}
                  className="px-3 py-1 bg-gray-100 text-gray-500 rounded text-xs hover:bg-gray-200"
                >
                  Close
                </button>
              </div>

              <textarea
                value={promptDraft}
                onChange={(e) => setPromptDraft(e.target.value)}
                className="w-full border rounded p-3 text-xs font-mono leading-relaxed"
                rows={18}
                spellCheck={false}
              />

              <div className="flex gap-2">
                <button
                  onClick={async () => {
                    await handleUpdate(activePrompt, promptDraft);
                    showMessage("Prompt saved to database", "success");
                  }}
                  className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700"
                >
                  Save Template
                </button>
                <button
                  onClick={async () => {
                    try {
                      const result = await resetPrompt(activePrompt);
                      setPromptDraft(result.value);
                      setSettings((prev) => ({ ...prev, [activePrompt]: result.value }));
                      showMessage("Reset to default", "success");
                    } catch (err: any) {
                      showMessage(err.message, "error");
                    }
                  }}
                  className="px-4 py-2 bg-gray-200 text-gray-700 rounded text-sm hover:bg-gray-300"
                >
                  Reset to Default
                </button>
              </div>
            </div>
          )}

          {/* API Keys — vertical list at bottom */}
          <div className="border rounded-lg p-5 space-y-4">
            <h2 className="font-semibold text-lg">API Keys</h2>
            <p className="text-xs text-gray-400">Encrypted at rest. Decrypted only at LLM call time.</p>
            <div className="space-y-3">
              {[
                { key: "api_key_anthropic", label: "Anthropic", desc: "Used by Clinical Extraction + Pathway Q&A" },
                { key: "api_key_google", label: "Google (Gemini)", desc: "Fallback provider for evaluation + extraction" },
                { key: "api_key_openai", label: "OpenAI", desc: "Used by RAG Embeddings (text-embedding-3)" },
              ].map(({ key, label, desc }) => (
                <div key={key} className="flex items-center gap-3 border-b pb-3 last:border-0 last:pb-0">
                  <div className="w-40">
                    <span className={`inline-block w-2 h-2 rounded-full mr-2 ${settings[key] ? "bg-green-500" : "bg-red-400"}`} />
                    <span className="text-sm font-medium">{label}</span>
                    <p className="text-xs text-gray-400 ml-4">{desc}</p>
                  </div>
                  <input
                    type="password"
                    value={apiKeyDrafts[key] ?? ""}
                    onChange={(e) =>
                      setApiKeyDrafts((prev) => ({ ...prev, [key]: e.target.value }))
                    }
                    className="border rounded px-2 py-1.5 text-sm flex-1 font-mono"
                    placeholder={settings[key] || "Not set"}
                  />
                  <button
                    onClick={async () => {
                      if (apiKeyDrafts[key]) {
                        await handleUpdate(key, apiKeyDrafts[key]);
                        setApiKeyDrafts((prev) => ({ ...prev, [key]: "" }));
                        showMessage(`${label} key saved`, "success");
                      }
                    }}
                    className="px-3 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700"
                  >
                    Save
                  </button>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* ═══════════════════════════════════════════════════ */}
      {/* WORKERS TAB                                        */}
      {/* ═══════════════════════════════════════════════════ */}
      {activeTab === "Workers" && (
        <div className="space-y-6">
          <div className="border rounded-lg p-5 space-y-4">
            <h2 className="font-semibold text-lg">Worker Accounts</h2>
            <p className="text-xs text-gray-400">
              Carelon portal login accounts. Each worker runs on a separate VM with its own browser.
              Cases are round-robined across active workers.
            </p>

            {/* Add Worker Form */}
            <form
              className="flex gap-2 items-end flex-wrap"
              onSubmit={async (e) => {
                e.preventDefault();
                const form = e.target as HTMLFormElement;
                const fd = new FormData(form);
                try {
                  const { createWorkerAccount } = await import("@/lib/api");
                  await createWorkerAccount({
                    container_id: fd.get("container_id") as string,
                    username: fd.get("username") as string,
                    password: fd.get("password") as string,
                    worker_url: fd.get("worker_url") as string || undefined,
                    mailbox_address: fd.get("mailbox_address") as string,
                    shift: fd.get("shift") as string || "DAY",
                    job_type: fd.get("job_type") as string || "FIRST_PASS",
                  });
                  showMessage("Worker added", "success");
                  form.reset();
                  refresh();
                } catch (err: any) {
                  showMessage(err.message, "error");
                }
              }}
            >
              <div className="flex flex-col">
                <label className="text-xs text-gray-500">Worker ID</label>
                <input name="container_id" required placeholder="worker-a" className="border rounded px-2 py-1 text-sm w-28" />
              </div>
              <div className="flex flex-col">
                <label className="text-xs text-gray-500">Username</label>
                <input name="username" required placeholder="carelon_user" className="border rounded px-2 py-1 text-sm w-36" />
              </div>
              <div className="flex flex-col">
                <label className="text-xs text-gray-500">Password</label>
                <input name="password" type="password" required placeholder="********" className="border rounded px-2 py-1 text-sm w-32" />
              </div>
              <div className="flex flex-col">
                <label className="text-xs text-gray-500">Worker URL</label>
                <input name="worker_url" placeholder="http://10.0.0.X:9081" className="border rounded px-2 py-1 text-sm w-44" />
              </div>
              <div className="flex flex-col">
                <label className="text-xs text-gray-500">MFA Mailbox</label>
                <input name="mailbox_address" required placeholder="worker@domain.com" className="border rounded px-2 py-1 text-sm w-44" />
              </div>
              <div className="flex flex-col">
                <label className="text-xs text-gray-500">Shift</label>
                <select name="shift" className="border rounded px-2 py-1 text-sm w-20">
                  <option value="DAY">DAY</option>
                  <option value="NIGHT">NIGHT</option>
                </select>
              </div>
              <div className="flex flex-col">
                <label className="text-xs text-gray-500">Role</label>
                <select name="job_type" className="border rounded px-2 py-1 text-sm w-32">
                  <option value="FIRST_PASS">First Pass</option>
                  <option value="SUBMIT">Submission</option>
                </select>
              </div>
              <button type="submit" className="bg-blue-600 text-white text-sm px-3 py-1 rounded hover:bg-blue-700">
                Add Worker
              </button>
            </form>

            {/* Workers Table */}
            {workerAccounts.length > 0 && (
              <div className="overflow-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b text-left text-gray-500">
                      <th className="py-2 px-2">Worker ID</th>
                      <th className="py-2 px-2">Username</th>
                      <th className="py-2 px-2">Password</th>
                      <th className="py-2 px-2">Worker URL</th>
                      <th className="py-2 px-2">Mailbox</th>
                      <th className="py-2 px-2">Shift</th>
                      <th className="py-2 px-2">Role</th>
                      <th className="py-2 px-2">Cases</th>
                      <th className="py-2 px-2">Status</th>
                      <th className="py-2 px-2">Active</th>
                      <th className="py-2 px-2">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {workerAccounts.map((w: any) => {
                      const isEditing = editingWorkerId === w.id;
                      const ed = workerEditData;

                      return (
                        <tr key={w.id} className="border-b hover:bg-gray-50">
                          <td className="py-2 px-2 font-mono text-xs">{w.container_id}</td>
                          <td className="py-2 px-2">
                            {isEditing ? (
                              <input value={ed.username || ""} onChange={(e) => setWorkerEditData({...ed, username: e.target.value})} className="border rounded px-1 py-0.5 text-xs w-36" />
                            ) : <span className="text-xs">{w.username}</span>}
                          </td>
                          <td className="py-2 px-2">
                            {isEditing ? (
                              <input type="password" value={ed.password || ""} onChange={(e) => setWorkerEditData({...ed, password: e.target.value})} placeholder="(unchanged)" className="border rounded px-1 py-0.5 text-xs w-24" />
                            ) : (
                              <span className="text-xs text-gray-400">********</span>
                            )}
                          </td>
                          <td className="py-2 px-2">
                            {isEditing ? (
                              <input value={ed.worker_url || ""} onChange={(e) => setWorkerEditData({...ed, worker_url: e.target.value})} placeholder="http://10.0.0.X:9081" className="border rounded px-1 py-0.5 text-xs w-40" />
                            ) : (
                              <span className={`text-xs font-mono ${w.worker_url ? "text-gray-400" : "text-red-400 font-medium"}`}>{w.worker_url || "NOT SET"}</span>
                            )}
                          </td>
                          <td className="py-2 px-2">
                            {isEditing ? (
                              <input value={ed.mailbox_address || ""} onChange={(e) => setWorkerEditData({...ed, mailbox_address: e.target.value})} className="border rounded px-1 py-0.5 text-xs w-40" />
                            ) : (
                              <span className="text-xs text-gray-400">{w.mailbox_address || "\u2014"}</span>
                            )}
                          </td>
                          <td className="py-2 px-2">
                            {isEditing ? (
                              <select value={ed.shift || "DAY"} onChange={(e) => setWorkerEditData({...ed, shift: e.target.value})} className="border rounded px-1 py-0.5 text-xs">
                                <option value="DAY">DAY</option>
                                <option value="NIGHT">NIGHT</option>
                              </select>
                            ) : <span className="text-xs">{w.shift}</span>}
                          </td>
                          <td className="py-2 px-2">
                            {isEditing ? (
                              <select value={ed.job_type || "FIRST_PASS"} onChange={(e) => setWorkerEditData({...ed, job_type: e.target.value})} className="border rounded px-1 py-0.5 text-xs">
                                <option value="FIRST_PASS">First Pass</option>
                                <option value="SUBMIT">Submission</option>
                              </select>
                            ) : (
                              <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${
                                w.job_type === "SUBMIT"
                                  ? "bg-purple-100 text-purple-700"
                                  : "bg-blue-100 text-blue-700"
                              }`}>
                                {w.job_type === "SUBMIT" ? "Submission" : "First Pass"}
                              </span>
                            )}
                          </td>
                          <td className="py-2 px-2 text-xs">{w.cases_today}/{w.total_cases}</td>
                          <td className="py-2 px-2">
                            {w.is_logged_in ? (
                              <span className="text-xs bg-green-100 text-green-700 px-2 py-0.5 rounded">Online</span>
                            ) : (
                              <span className="text-xs bg-gray-100 text-gray-500 px-2 py-0.5 rounded">Offline</span>
                            )}
                          </td>
                          <td className="py-2 px-2">
                            <button
                              onClick={async () => {
                                const { toggleWorkerAccount } = await import("@/lib/api");
                                await toggleWorkerAccount(w.id);
                                refresh();
                              }}
                              className={`text-xs px-2 py-0.5 rounded ${
                                w.is_active
                                  ? "bg-green-100 text-green-700 hover:bg-green-200"
                                  : "bg-gray-100 text-gray-500 hover:bg-gray-200"
                              }`}
                            >
                              {w.is_active ? "ON" : "OFF"}
                            </button>
                          </td>
                          <td className="py-2 px-2 flex gap-1">
                            {isEditing ? (
                              <>
                                <button
                                  onClick={async () => {
                                    try {
                                      const { updateWorkerAccount } = await import("@/lib/api");
                                      const updates: any = {
                                        username: ed.username,
                                        worker_url: ed.worker_url,
                                        mailbox_address: ed.mailbox_address,
                                        shift: ed.shift,
                                        job_type: ed.job_type,
                                      };
                                      if (ed.password) updates.password = ed.password;
                                      await updateWorkerAccount(w.id, updates);
                                      showMessage("Worker updated", "success");
                                      setEditingWorkerId(null);
                                      setWorkerEditData({});
                                      refresh();
                                    } catch (err: any) {
                                      showMessage(err.message, "error");
                                    }
                                  }}
                                  className="text-xs text-green-600 hover:text-green-800 font-medium"
                                >
                                  Save
                                </button>
                                <button onClick={() => { setEditingWorkerId(null); setWorkerEditData({}); }} className="text-xs text-gray-400 hover:text-gray-600">
                                  Cancel
                                </button>
                              </>
                            ) : (
                              <>
                                <button
                                  onClick={() => {
                                    setEditingWorkerId(w.id);
                                    setWorkerEditData({
                                      username: w.username,
                                      password: "",
                                      worker_url: w.worker_url || "",
                                      mailbox_address: w.mailbox_address || "",
                                      shift: w.shift || "DAY",
                                      job_type: w.job_type || "FIRST_PASS",
                                    });
                                  }}
                                  className="text-xs text-blue-500 hover:text-blue-700"
                                >
                                  Edit
                                </button>
                                <button
                                  onClick={async () => {
                                    if (!confirm(`Delete worker ${w.container_id}?`)) return;
                                    const { deleteWorkerAccount } = await import("@/lib/api");
                                    await deleteWorkerAccount(w.id);
                                    showMessage("Worker deleted", "success");
                                    refresh();
                                  }}
                                  className="text-xs text-red-500 hover:text-red-700"
                                >
                                  Delete
                                </button>
                              </>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ═══════════════════════════════════════════════════ */}
      {/* AUTOMATION TAB                                     */}
      {/* ═══════════════════════════════════════════════════ */}
      {activeTab === "Automation" && (
        <div className="space-y-6">
          <AutomationRulesSection showMessage={showMessage} />
        </div>
      )}

      {/* ═══════════════════════════════════════════════════ */}
      {/* INTEGRATIONS TAB                                   */}
      {/* ═══════════════════════════════════════════════════ */}
      {activeTab === "Integrations" && (
        <div className="space-y-6">
          <div className="border rounded-lg p-5 space-y-4">
            <h2 className="font-semibold text-lg">RingCentral Fax</h2>
            <p className="text-xs text-gray-400">
              Configure RingCentral integration for sending fax notifications to Carelon.
            </p>

            <div className="space-y-4 max-w-xl">
              {/* Fax Enabled */}
              <div className="flex items-center justify-between">
                <label className="text-sm font-medium">Fax Enabled</label>
                <button
                  onClick={() => setRcSettings((prev) => ({ ...prev, fax_enabled: !prev.fax_enabled }))}
                  className={`px-3 py-1 rounded text-sm font-medium transition ${
                    rcSettings.fax_enabled
                      ? "bg-green-100 text-green-700"
                      : "bg-gray-100 text-gray-500"
                  }`}
                >
                  {rcSettings.fax_enabled ? "ON" : "OFF"}
                </button>
              </div>

              {/* Client ID */}
              <div className="space-y-1">
                <label className="text-sm text-gray-600">Client ID</label>
                <input
                  type="text"
                  value={rcSettings.client_id || ""}
                  onChange={(e) => setRcSettings((prev) => ({ ...prev, client_id: e.target.value }))}
                  className="w-full border rounded px-3 py-2 text-sm font-mono"
                  placeholder="RingCentral Client ID"
                />
              </div>

              {/* Client Secret */}
              <div className="space-y-1">
                <label className="text-sm text-gray-600">Client Secret</label>
                <input
                  type="password"
                  value={rcSettings.client_secret || ""}
                  onChange={(e) => setRcSettings((prev) => ({ ...prev, client_secret: e.target.value }))}
                  className="w-full border rounded px-3 py-2 text-sm font-mono"
                  placeholder="RingCentral Client Secret"
                />
              </div>

              {/* JWT Token */}
              <div className="space-y-1">
                <label className="text-sm text-gray-600">JWT Token</label>
                <input
                  type="password"
                  value={rcSettings.jwt_token || ""}
                  onChange={(e) => setRcSettings((prev) => ({ ...prev, jwt_token: e.target.value }))}
                  className="w-full border rounded px-3 py-2 text-sm font-mono"
                  placeholder="RingCentral JWT Token"
                />
              </div>

              {/* Server URL */}
              <div className="space-y-1">
                <label className="text-sm text-gray-600">Server URL</label>
                <input
                  type="text"
                  value={rcSettings.server_url || "https://platform.ringcentral.com"}
                  onChange={(e) => setRcSettings((prev) => ({ ...prev, server_url: e.target.value }))}
                  className="w-full border rounded px-3 py-2 text-sm font-mono"
                  placeholder="https://platform.ringcentral.com"
                />
              </div>

              {/* Carelon Fax Number */}
              <div className="space-y-1">
                <label className="text-sm text-gray-600">Carelon Fax Number</label>
                <input
                  type="text"
                  value={rcSettings.carelon_fax_number || "+18007982068"}
                  onChange={(e) => setRcSettings((prev) => ({ ...prev, carelon_fax_number: e.target.value }))}
                  className="w-full border rounded px-3 py-2 text-sm font-mono"
                  placeholder="+18007982068"
                />
              </div>

              {/* Action Buttons */}
              <div className="flex gap-2 pt-2">
                <button
                  onClick={handleTestRcConnection}
                  disabled={rcTesting}
                  className="px-4 py-2 bg-gray-600 text-white rounded text-sm hover:bg-gray-700 disabled:opacity-50"
                >
                  {rcTesting ? "Testing..." : "Test Connection"}
                </button>
                <button
                  onClick={handleSaveRcSettings}
                  disabled={rcSaving}
                  className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50"
                >
                  {rcSaving ? "Saving..." : "Save"}
                </button>
              </div>
            </div>
          </div>

          {/* ─── Availity Eligibility ─── */}
          <div className="border rounded-lg p-5 space-y-4">
            <h2 className="font-semibold text-lg">Availity Eligibility</h2>
            <p className="text-xs text-gray-400">
              Used to resolve MEMBER_NOT_FOUND cases. Queries coverage + auth_org_name
              via the Ronexa-hosted Availity proxy API.
            </p>

            <div className="space-y-4 max-w-xl">
              {/* Auto-send toggle */}
              <div className="flex items-center justify-between">
                <label className="text-sm font-medium">
                  Auto-send MEMBER_NOT_FOUND cases
                </label>
                <button
                  onClick={() =>
                    setAvailitySettings((prev) => ({
                      ...prev,
                      availity_auto_send_enabled: !prev.availity_auto_send_enabled,
                    }))
                  }
                  className={`px-3 py-1 rounded text-sm font-medium transition ${
                    availitySettings.availity_auto_send_enabled
                      ? "bg-green-100 text-green-700"
                      : "bg-gray-100 text-gray-500"
                  }`}
                >
                  {availitySettings.availity_auto_send_enabled ? "ON" : "OFF"}
                </button>
              </div>

              {/* Interval (only when auto-send is on) */}
              {availitySettings.availity_auto_send_enabled && (
                <div className="space-y-1">
                  <label className="text-sm text-gray-600">
                    Auto-send interval (minutes, min 30)
                  </label>
                  <input
                    type="number"
                    min={30}
                    value={availitySettings.availity_auto_send_interval_minutes || 30}
                    onChange={(e) =>
                      setAvailitySettings((prev) => ({
                        ...prev,
                        availity_auto_send_interval_minutes: Math.max(
                          30,
                          parseInt(e.target.value, 10) || 30
                        ),
                      }))
                    }
                    className="w-full border rounded px-3 py-2 text-sm font-mono"
                  />
                  <p className="text-[11px] text-gray-400">
                    Not yet wired — toggle is stored but scheduled runs are Phase 2.
                  </p>
                </div>
              )}

              {/* Username */}
              <div className="space-y-1">
                <label className="text-sm text-gray-600">Username</label>
                <input
                  type="text"
                  value={availitySettings.availity_username || ""}
                  onChange={(e) =>
                    setAvailitySettings((prev) => ({
                      ...prev,
                      availity_username: e.target.value,
                    }))
                  }
                  className="w-full border rounded px-3 py-2 text-sm font-mono"
                  placeholder="Availity login username"
                />
              </div>

              {/* Password */}
              <div className="space-y-1">
                <label className="text-sm text-gray-600">Password</label>
                <input
                  type="password"
                  value={availitySettings.availity_password || ""}
                  onChange={(e) =>
                    setAvailitySettings((prev) => ({
                      ...prev,
                      availity_password: e.target.value,
                    }))
                  }
                  className="w-full border rounded px-3 py-2 text-sm font-mono"
                  placeholder="Availity login password"
                />
              </div>

              {/* Base URL */}
              <div className="space-y-1">
                <label className="text-sm text-gray-600">Base URL</label>
                <input
                  type="text"
                  value={availitySettings.availity_base_url || "https://api-v2.ronexa.com"}
                  onChange={(e) =>
                    setAvailitySettings((prev) => ({
                      ...prev,
                      availity_base_url: e.target.value,
                    }))
                  }
                  className="w-full border rounded px-3 py-2 text-sm font-mono"
                  placeholder="https://api-v2.ronexa.com"
                />
              </div>

              {/* Action Buttons */}
              <div className="flex gap-2 pt-2">
                <button
                  onClick={handleTestAvailityConnection}
                  disabled={availityTesting}
                  className="px-4 py-2 bg-gray-600 text-white rounded text-sm hover:bg-gray-700 disabled:opacity-50"
                >
                  {availityTesting ? "Testing..." : "Test Connection"}
                </button>
                <button
                  onClick={handleSaveAvailitySettings}
                  disabled={availitySaving}
                  className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50"
                >
                  {availitySaving ? "Saving..." : "Save"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function formatTimestamp(iso: string): string {
  if (!iso || iso === "null") return "-";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

// ── Model options per provider ──

const MODEL_OPTIONS: Record<string, Record<string, string[]>> = {
  anthropic: {
    eval: ["claude-sonnet-4-6", "claude-sonnet-4-5-20241022", "claude-3-5-sonnet-20241022", "claude-opus-4-6"],
    extract: ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-3-5-haiku-20241022"],
    match: ["claude-haiku-4-5-20251001", "claude-3-5-haiku-20241022"],
  },
  google: {
    eval: ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"],
    extract: ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-1.5-flash"],
    match: ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-1.5-flash"],
  },
};

function WorkflowNode({
  title,
  subtitle,
  providerKey,
  modelKey,
  role,
  promptKeys,
  settings,
  apiKeyDrafts,
  onUpdate,
  onEditPrompt,
}: {
  title: string;
  subtitle: string;
  providerKey: string;
  modelKey: string;
  role: "eval" | "extract" | "match";
  promptKeys: string[];
  settings: Record<string, any>;
  apiKeyDrafts: Record<string, string>;
  onUpdate: (key: string, value: any) => void;
  onEditPrompt: (key: string) => void;
}) {
  const provider = settings[providerKey] || "google";
  const model = settings[modelKey] || "";
  const keyField = provider === "anthropic" ? "api_key_anthropic" : "api_key_google";
  const hasKey = !!(settings[keyField]);
  const models = MODEL_OPTIONS[provider]?.[role] || [];
  const borderColor = provider === "anthropic" ? "border-blue-200" : "border-amber-200";
  const bgColor = provider === "anthropic" ? "bg-blue-50/30" : "bg-amber-50/30";

  return (
    <div className={`border-2 ${borderColor} rounded-lg p-4 flex-1 min-w-[180px] ${bgColor}`}>
      <div className="flex items-center gap-2 mb-1">
        <span className={`w-2 h-2 rounded-full ${hasKey ? "bg-green-500" : "bg-red-400"}`} />
        <h3 className="text-sm font-semibold">{title}</h3>
      </div>
      <p className="text-xs text-gray-400 mb-3">{subtitle}</p>

      <div className="space-y-2">
        <div>
          <label className="text-xs text-gray-500">Provider</label>
          <select
            value={provider}
            onChange={(e) => {
              onUpdate(providerKey, e.target.value);
              // Auto-set first model for new provider
              const newModels = MODEL_OPTIONS[e.target.value]?.[role] || [];
              if (newModels.length > 0) {
                onUpdate(modelKey, newModels[0]);
              }
            }}
            className="w-full border rounded px-2 py-1 text-xs"
          >
            <option value="anthropic">Anthropic</option>
            <option value="google">Google</option>
          </select>
        </div>

        <div>
          <label className="text-xs text-gray-500">Model</label>
          <select
            value={models.includes(model) ? model : "__custom"}
            onChange={(e) => {
              if (e.target.value !== "__custom") {
                onUpdate(modelKey, e.target.value);
              }
            }}
            className="w-full border rounded px-2 py-1 text-xs font-mono"
          >
            {models.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
            {!models.includes(model) && model && (
              <option value="__custom">{model} (custom)</option>
            )}
          </select>
          {/* Custom model input if not in list */}
          {!models.includes(model) && (
            <input
              type="text"
              value={model}
              onChange={(e) => onUpdate(modelKey, e.target.value)}
              className="w-full border rounded px-2 py-1 text-xs font-mono mt-1"
              placeholder="Custom model name"
            />
          )}
        </div>

        <div className="flex gap-1 pt-1">
          {promptKeys.map((pk) => (
            <button
              key={pk}
              onClick={() => onEditPrompt(pk)}
              className="flex-1 px-2 py-1 bg-white border rounded text-xs text-gray-600 hover:bg-gray-50 hover:border-blue-300"
            >
              {pk.includes("system") ? "System" : "User"} Prompt
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}


// ── Automation Rules Component ──

function AutomationRulesSection({ showMessage }: { showMessage: (text: string, type: "success" | "error") => void }) {
  const [rules, setRules] = useState<any[]>([]);
  const [newCpt, setNewCpt] = useState("");
  const [newIcd, setNewIcd] = useState("");

  const loadRules = async () => {
    try {
      const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL || ""}/api/settings/automation-rules`);
      if (res.ok) setRules(await res.json());
    } catch {}
  };

  useEffect(() => { loadRules(); }, []);

  const addRule = async () => {
    if (!newCpt || !newIcd) return;
    try {
      await fetch(`${process.env.NEXT_PUBLIC_API_URL || ""}/api/settings/automation-rules`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cpt_code: newCpt, icd_code: newIcd, enabled: false }),
      });
      setNewCpt("");
      setNewIcd("");
      showMessage("Rule added", "success");
      loadRules();
    } catch (err: any) {
      showMessage(err.message, "error");
    }
  };

  const toggleRule = async (ruleId: string, enabled: boolean) => {
    try {
      await fetch(`${process.env.NEXT_PUBLIC_API_URL || ""}/api/settings/automation-rules/${ruleId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled, enabled_by: "operator" }),
      });
      showMessage(enabled ? "Automation ENABLED" : "Automation DISABLED", "success");
      loadRules();
    } catch (err: any) {
      showMessage(err.message, "error");
    }
  };

  const deleteRule = async (ruleId: string) => {
    try {
      await fetch(`${process.env.NEXT_PUBLIC_API_URL || ""}/api/settings/automation-rules/${ruleId}`, { method: "DELETE" });
      showMessage("Rule deleted", "success");
      loadRules();
    } catch (err: any) {
      showMessage(err.message, "error");
    }
  };

  return (
    <div className="border rounded-lg p-5 space-y-4">
      <h2 className="font-semibold text-lg">Full Automation Rules</h2>
      <p className="text-xs text-gray-400">
        CPT+ICD combos with automation ON skip L1+L2 review — LLM answers go straight to portal submission.
        Use Analytics to build confidence before enabling.
      </p>

      <div className="flex gap-2 items-end">
        <div>
          <label className="text-xs text-gray-500 block">CPT Code</label>
          <input type="text" value={newCpt} onChange={(e) => setNewCpt(e.target.value)}
            placeholder="e.g. 72148" className="border rounded px-2 py-1 text-sm w-28" />
        </div>
        <div>
          <label className="text-xs text-gray-500 block">ICD-10</label>
          <input type="text" value={newIcd} onChange={(e) => setNewIcd(e.target.value)}
            placeholder="e.g. M54.5" className="border rounded px-2 py-1 text-sm w-28" />
        </div>
        <button onClick={addRule} disabled={!newCpt || !newIcd}
          className="bg-blue-600 text-white px-3 py-1 rounded text-sm disabled:opacity-50 hover:bg-blue-700">
          Add Rule
        </button>
      </div>

      {rules.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-gray-500 border-b">
                <th className="py-2 pr-3">CPT</th>
                <th className="py-2 pr-3">ICD-10</th>
                <th className="py-2 pr-3">Total</th>
                <th className="py-2 pr-3">Approved</th>
                <th className="py-2 pr-3">Denied</th>
                <th className="py-2 pr-3">Pended</th>
                <th className="py-2 pr-3">Success %</th>
                <th className="py-2 pr-3">Auto</th>
                <th className="py-2"></th>
              </tr>
            </thead>
            <tbody>
              {rules.map((rule: any) => {
                const s = rule.stats || {};
                const rate = s.total > 0 ? Math.round((s.approved / s.total) * 100) : 0;
                return (
                  <tr key={rule.id} className="border-b hover:bg-gray-50">
                    <td className="py-2 pr-3 font-mono">{rule.cpt_code}</td>
                    <td className="py-2 pr-3 font-mono">{rule.icd_code}</td>
                    <td className="py-2 pr-3">{s.total || 0}</td>
                    <td className="py-2 pr-3 text-green-600">{s.approved || 0}</td>
                    <td className="py-2 pr-3 text-red-600">{s.denied || 0}</td>
                    <td className="py-2 pr-3 text-amber-600">{s.pended || 0}</td>
                    <td className="py-2 pr-3">
                      <span className={`font-medium ${rate >= 90 ? "text-green-600" : rate >= 70 ? "text-amber-600" : "text-red-600"}`}>
                        {s.total > 0 ? `${rate}%` : "\u2014"}
                      </span>
                    </td>
                    <td className="py-2 pr-3">
                      <button onClick={() => toggleRule(rule.id, !rule.enabled)}
                        className={`px-3 py-0.5 rounded text-xs font-medium ${
                          rule.enabled ? "bg-green-100 text-green-700 hover:bg-green-200" : "bg-gray-100 text-gray-500 hover:bg-gray-200"
                        }`}>
                        {rule.enabled ? "ON" : "OFF"}
                      </button>
                    </td>
                    <td className="py-2">
                      <button onClick={() => deleteRule(rule.id)} className="text-red-400 hover:text-red-600 text-xs">{"\u2715"}</button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-sm text-gray-400">No automation rules yet. Add a CPT+ICD combo above.</p>
      )}
    </div>
  );
}
