"use client";

import { useEffect, useMemo, useState } from "react";
import { fetchAvailityCases } from "../actions";
import { AvailityCase } from "../types";
import StatusBadge from "./StatusBadge";
import EligibilityDetail from "./EligibilityDetail";
import {
  getAvailityWorklistFilters,
  listAvailityWorklist,
} from "../lib/worklistApi";
import type { AvailityWorklistFilter } from "../lib/worklistTypes";

const columns = [
  "CLAIM ID",
  "STATUS",
  "ELIGIBILITY",
  "REFERRAL",
  "PATIENT",
  "DOB",
  "GENDER",
  "MEMBER ID",
  "SUBSCRIBER",
  "PAYER",
  "PAYER ID",
  "PORTAL",
  "INSURANCE",
  "INSURANCE TYPE",
  "GROUP #",
  "GROUP NAME",
  "PLAN STATUS",
  "COVERAGE START",
  "COVERAGE END",
  "AUTH REQ",
  "EXTRACTED",
];

function formatFilterLabel(value: string) {
  if (!value) return "All Worklist Items";
  return value
    .replaceAll("_", " ")
    .toLowerCase()
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

export default function AvailityTable() {
  const [rows, setRows] = useState<AvailityCase[]>([]);
  const [filters, setFilters] = useState<AvailityWorklistFilter[]>([]);
  const [selectedFilter, setSelectedFilter] = useState("");
  const [filterOpen, setFilterOpen] = useState(false);
  const [caseIdSearch, setCaseIdSearch] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize] = useState(20);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);

  const allFilterCount = useMemo(
    () => filters.reduce((sum, filter) => sum + filter.count, 0),
    [filters]
  );

  async function loadCases(filter = selectedFilter, search = caseIdSearch) {
    setLoading(true);

    try {
      const filterList = await getAvailityWorklistFilters();
      setFilters(filterList);

      let examIds: string[] | undefined;

      if (filter) {
        const worklist = await listAvailityWorklist({
          exceptionType: filter,
          page: 1,
          pageSize: 200,
        });

        examIds = worklist.rows.map((row) => row.exam_id).filter(Boolean);
      }

      const data = await fetchAvailityCases(page, pageSize, search, examIds);

      setRows(data.items);
      setTotal(data.total);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadCases();
  }, [page, selectedFilter]);

  function handleFilterSelect(key: string) {
    setSelectedFilter(key);
    setExpanded(null);
    setFilterOpen(false);
    setPage(1);
  }

  function handleSearchSubmit(e: React.FormEvent) {
    e.preventDefault();
    setExpanded(null);
    setPage(1);
    loadCases(selectedFilter, caseIdSearch);
  }

  function handleClearFilters() {
    setSelectedFilter("");
    setCaseIdSearch("");
    setExpanded(null);
    setPage(1);
    loadCases("", "");
  }

  const selectedFilterCount =
    filters.find((filter) => filter.key === selectedFilter)?.count ?? allFilterCount;

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div className="w-full space-y-5">
      <div className="flex flex-wrap items-end gap-4">
        <div className="relative">
          <label className="mb-1 block text-xs font-medium text-gray-600">
            Filter
          </label>

          <button
            type="button"
            onClick={() => setFilterOpen((open) => !open)}
            className="flex min-w-[260px] items-center justify-between rounded-lg border bg-white px-4 py-2 text-sm shadow-sm hover:bg-gray-50"
          >
            <span>{formatFilterLabel(selectedFilter)}</span>
            <span className="ml-3 rounded bg-gray-100 px-2 py-0.5 text-xs font-semibold text-gray-700">
              {selectedFilterCount}
            </span>
          </button>

          {filterOpen && (
            <div className="absolute z-20 mt-2 w-[300px] rounded-xl border bg-white p-2 shadow-lg">
              <p className="px-3 py-2 text-xs font-semibold uppercase text-gray-400">
                Filter
              </p>

              <button
                type="button"
                onClick={() => handleFilterSelect("")}
                className={`flex w-full items-center justify-between rounded-lg px-3 py-2 text-left text-sm ${
                  selectedFilter === ""
                    ? "bg-blue-50 text-blue-700"
                    : "hover:bg-gray-50"
                }`}
              >
                <span>All Worklist Items</span>
                <span className="text-xs font-semibold">{allFilterCount}</span>
              </button>

              {filters.map((filter) => (
                <button
                  key={filter.key}
                  type="button"
                  onClick={() => handleFilterSelect(filter.key)}
                  className={`flex w-full items-center justify-between rounded-lg px-3 py-2 text-left text-sm ${
                    selectedFilter === filter.key
                      ? "bg-blue-50 text-blue-700"
                      : "hover:bg-gray-50"
                  }`}
                >
                  <span>{filter.label}</span>
                  <span className="text-xs font-semibold">{filter.count}</span>
                </button>
              ))}
            </div>
          )}
        </div>

        <form onSubmit={handleSearchSubmit} className="flex items-end gap-3">
          <div>
            <label className="mb-1 block text-xs font-medium text-gray-600">
              Case ID
            </label>
            <input
              value={caseIdSearch}
              onChange={(e) => setCaseIdSearch(e.target.value)}
              placeholder="Search Case ID..."
              className="w-72 rounded-lg border px-3 py-2 text-sm shadow-sm"
            />
          </div>

          <button
            type="submit"
            className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
          >
            Apply
          </button>

          <button
            type="button"
            onClick={handleClearFilters}
            className="rounded-lg border px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
          >
            Clear
          </button>
        </form>
      </div>

      <div className="flex items-center justify-between gap-4">
        <h2 className="text-lg font-semibold">Eligibility Result</h2>
        <p className="text-sm text-gray-500">
          {total} result{total === 1 ? "" : "s"}
        </p>
      </div>

      <div className="w-full overflow-hidden rounded-lg border bg-white">
        <div className="w-full overflow-x-auto">
          <table className="w-full min-w-[2200px] text-xs">
            <thead className="border-b bg-gray-50 text-left uppercase text-gray-500">
              <tr>
                {columns.map((column) => (
                  <th
                    key={column}
                    className="whitespace-nowrap px-3 py-2 font-medium"
                  >
                    {column}
                  </th>
                ))}
              </tr>
            </thead>

            <tbody>
              {loading && (
                <tr>
                  <td
                    colSpan={columns.length}
                    className="px-3 py-8 text-center text-gray-400"
                  >
                    Loading...
                  </td>
                </tr>
              )}

              {!loading && rows.length === 0 && (
                <tr>
                  <td
                    colSpan={columns.length}
                    className="px-3 py-8 text-center text-gray-400"
                  >
                    No cases found.
                  </td>
                </tr>
              )}

              {!loading &&
                rows.map((row) => (
                  <tbody key={row._id}>
                    <tr
                      onClick={() =>
                        setExpanded((current) =>
                          current === row._id ? null : row._id
                        )
                      }
                      className="cursor-pointer border-b hover:bg-gray-50"
                    >
                      <td className="whitespace-nowrap px-3 py-2 font-mono">
                        {row.claim_id}
                      </td>

                      <td className="whitespace-nowrap px-3 py-2">
                        <StatusBadge value={row.status} />
                      </td>

                      <td className="whitespace-nowrap px-3 py-2">
                        <StatusBadge value={row.eligibility} />
                      </td>

                      <td className="whitespace-nowrap px-3 py-2">
                        {row.referral || "-"}
                      </td>

                      <td className="whitespace-nowrap px-3 py-2">
                        {row.patient || "-"}
                      </td>

                      <td className="whitespace-nowrap px-3 py-2">
                        {row.dob || "-"}
                      </td>

                      <td className="whitespace-nowrap px-3 py-2">
                        {row.gender || "-"}
                      </td>

                      <td className="whitespace-nowrap px-3 py-2">
                        {row.member_id || "-"}
                      </td>

                      <td className="whitespace-nowrap px-3 py-2">
                        {row.subscriber || "-"}
                      </td>

                      <td className="whitespace-nowrap px-3 py-2">
                        {row.payer || "-"}
                      </td>

                      <td className="whitespace-nowrap px-3 py-2">
                        {row.payer_id || "-"}
                      </td>

                      <td className="whitespace-nowrap px-3 py-2">
                        {row.portal || "-"}
                      </td>

                      <td className="whitespace-nowrap px-3 py-2">
                        {row.insurance || "-"}
                      </td>

                      <td className="whitespace-nowrap px-3 py-2">
                        {row.insurance_type || "-"}
                      </td>

                      <td className="whitespace-nowrap px-3 py-2">
                        {row.group_number || "-"}
                      </td>

                      <td className="whitespace-nowrap px-3 py-2">
                        {row.group_name || "-"}
                      </td>

                      <td className="whitespace-nowrap px-3 py-2">
                        {row.plan_status || "-"}
                      </td>

                      <td className="whitespace-nowrap px-3 py-2">
                        {row.coverage_start || "-"}
                      </td>

                      <td className="whitespace-nowrap px-3 py-2">
                        {row.coverage_end || "-"}
                      </td>

                      <td className="whitespace-nowrap px-3 py-2">
                        {row.auth_req || ""}
                      </td>

                      <td className="whitespace-nowrap px-3 py-2">
                        {row.extracted}
                      </td>
                    </tr>

                    {expanded === row._id && (
                      <tr>
                        <td colSpan={columns.length} className="bg-gray-50 p-4">
                          <EligibilityDetail result={row.eligibility_result} />
                        </td>
                      </tr>
                    )}
                  </tbody>
                ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="flex items-center justify-between text-sm text-gray-600">
        <span>
          Page {page} of {totalPages} · {total} total
        </span>

        <div className="flex gap-2">
          <button
            disabled={page === 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            className="rounded border px-3 py-1 disabled:opacity-40"
          >
            Prev
          </button>

          <button
            disabled={page >= totalPages}
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            className="rounded border px-3 py-1 disabled:opacity-40"
          >
            Next
          </button>
        </div>
      </div>
    </div>
  );
}