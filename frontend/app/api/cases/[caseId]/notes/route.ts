import { NextRequest, NextResponse } from "next/server";

// Allow uploads up to 10MB
export const runtime = "nodejs";

export async function POST(
  request: NextRequest,
  { params }: { params: { caseId: string } }
) {
  const apiUrl = process.env.API_URL || "http://localhost:8000";
  const { caseId } = params;

  try {
    // Stream the request body directly to the backend
    const contentType = request.headers.get("content-type") || "";
    const cookie = request.headers.get("cookie") || "";

    const backendRes = await fetch(`${apiUrl}/api/cases/${caseId}/notes`, {
      method: "POST",
      headers: {
        "content-type": contentType,
        ...(cookie ? { cookie } : {}),
      },
      body: request.body,
      // @ts-ignore — duplex needed for streaming request body
      duplex: "half",
    });

    const data = await backendRes.json().catch(() => ({
      detail: backendRes.statusText,
    }));

    return NextResponse.json(data, { status: backendRes.status });
  } catch (err: any) {
    return NextResponse.json(
      { detail: err.message || "Upload proxy failed" },
      { status: 502 }
    );
  }
}
