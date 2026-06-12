"use server";

import { getDb } from "./lib/mongo";
import { mapCaseToRow } from "./utils";
import type { FetchAvailityResponse } from "./types";

export async function fetchAvailityCases(
  page = 1,
  pageSize = 20,
  search = "",
  examIds?: string[]
): Promise<FetchAvailityResponse> {
  const db = await getDb();

  const collection = db.collection(
    process.env.MONGO_COLLECTION_PROD || "gateway_submissions"
  );

  const baseQuery: any = {
    workflow_id: "carelon",
    workflow_type: "need_auth",
  };

  if (examIds && examIds.length > 0) {
    baseQuery.exam_id = {
      $in: examIds,
    };
  }

  if (search.trim()) {
    const searchValue = search.trim();

    baseQuery.$or = [
      {
        case_id: {
          $regex: searchValue,
          $options: "i",
        },
      },
      {
        exam_id: {
          $regex: searchValue,
          $options: "i",
        },
      },
      {
        "eligibility_result.patient_name": {
          $regex: searchValue,
          $options: "i",
        },
      },
      {
        "eligibility_result.member_id": {
          $regex: searchValue,
          $options: "i",
        },
      },
      {
        "eligibility_result.payer_name": {
          $regex: searchValue,
          $options: "i",
        },
      },
      {
        "eligibility_result.control_number": {
          $regex: searchValue,
          $options: "i",
        },
      },
    ];
  }

  const skip = (page - 1) * pageSize;

  const [items, total] = await Promise.all([
    collection
      .find(baseQuery)
      .sort({ created_at: -1 })
      .skip(skip)
      .limit(pageSize)
      .toArray(),

    collection.countDocuments(baseQuery),
  ]);

  return {
    items: items.map(mapCaseToRow),
    total,
    page,
    pageSize,
  };
}