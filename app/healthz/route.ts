export async function GET() {
  return Response.json({
    status: "ok",
    service: "netdisk-auto-sync-v2",
    timestamp: new Date().toISOString(),
  });
}
