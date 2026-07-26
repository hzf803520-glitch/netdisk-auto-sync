import { NextRequest, NextResponse } from "next/server";
import { execute, one, rows } from "@/lib/db";
import { encryptSecret } from "@/lib/secrets";

type Provider = "baidu" | "quark";

type ConfigRow = {
  provider: Provider;
  encrypted_cookie: string;
  cookie_hint: string;
  target_folder: string;
  folder_options: string;
  create_folder: boolean;
  updated_at: string;
};

function normalizeFolder(value: string) {
  const clean = value.trim().replace(/\\/g, "/").replace(/\/{2,}/g, "/");
  if (!clean) return "/影视资源库";
  return clean.startsWith("/") ? clean : `/${clean}`;
}

function normalizeCookie(value: string) {
  return value
    .trim()
    .replace(/^cookie\s*:\s*/i, "")
    .replace(/[\r\n]+/g, "; ")
    .replace(/;\s*;+/g, "; ");
}

function publicConfig(row?: ConfigRow) {
  if (!row) return { connected: false, cookieHint: "", targetFolder: "/影视资源库", folders: ["/影视资源库"], createFolder: true };
  let folders: string[] = [];
  try { folders = JSON.parse(row.folder_options); } catch { folders = [row.target_folder]; }
  return { connected: Boolean(row.encrypted_cookie), cookieHint: row.cookie_hint, targetFolder: row.target_folder, folders, createFolder: Boolean(row.create_folder), updatedAt: row.updated_at };
}

export async function GET() {
  const configs = await rows<ConfigRow>("SELECT * FROM provider_configs");
  const map = Object.fromEntries(configs.map((row) => [row.provider, publicConfig(row)]));
  return NextResponse.json({
    baidu: map.baidu || publicConfig(),
    quark: map.quark || publicConfig(),
  });
}

export async function POST(request: NextRequest) {
  const body = await request.json() as { provider?: Provider; cookie?: string; targetFolder?: string; createFolder?: boolean };
  if (body.provider !== "baidu" && body.provider !== "quark") return NextResponse.json({ error: "网盘类型无效" }, { status: 400 });
  const provider = body.provider;
  const existing = await one<ConfigRow>("SELECT * FROM provider_configs WHERE provider=$1", [provider]);
  const cookie = normalizeCookie(body.cookie || "");
  if (!cookie && !existing?.encrypted_cookie) return NextResponse.json({ error: "请输入 Cookie" }, { status: 400 });
  const targetFolder = normalizeFolder(body.targetFolder || existing?.target_folder || "/影视资源库");
  let folders: string[] = [];
  try { folders = existing ? JSON.parse(existing.folder_options) : []; } catch { folders = []; }
  folders = Array.from(new Set([targetFolder, ...folders])).slice(0, 30);
  const encryptedCookie = cookie ? encryptSecret(cookie) : existing?.encrypted_cookie || "";
  const cookieHint = cookie ? `${cookie.slice(0, 5)}••••${cookie.slice(-4)}` : existing?.cookie_hint || "";

  await execute(`
    INSERT INTO provider_configs (provider, encrypted_cookie, cookie_hint, target_folder, folder_options, create_folder, updated_at)
    VALUES ($1, $2, $3, $4, $5, $6, NOW())
    ON CONFLICT(provider) DO UPDATE SET
      encrypted_cookie=EXCLUDED.encrypted_cookie,
      cookie_hint=EXCLUDED.cookie_hint,
      target_folder=EXCLUDED.target_folder,
      folder_options=EXCLUDED.folder_options,
      create_folder=EXCLUDED.create_folder,
      updated_at=NOW()
  `, [provider, encryptedCookie, cookieHint, targetFolder, JSON.stringify(folders), body.createFolder !== false]);

  const saved = await one<ConfigRow>("SELECT * FROM provider_configs WHERE provider=$1", [provider]);
  return NextResponse.json({ ok: true, config: publicConfig(saved) });
}
