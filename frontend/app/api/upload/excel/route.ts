import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";

export async function POST(request: NextRequest) {
  const apiUrl = process.env.API_URL || "http://localhost:8000";

  try {
    const contentType = request.headers.get("content-type") || "";
    const cookie = request.headers.get("cookie") || "";

    const backendRes = await fetch(`${apiUrl}/api/upload/excel`, {
      method: "POST",
      headers: {
        "content-type": contentType,
        ...(cookie ? { cookie } : {}),
      },
      body: request.body,
      // @ts-ignore
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
