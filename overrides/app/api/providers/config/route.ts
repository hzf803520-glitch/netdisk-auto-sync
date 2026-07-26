import { NextRequest, NextResponse } from "next/server";
import { execute, one, rows } from "@/lib/db";
import { encryptSecret } from "@/lib/secrets";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

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

type SaveBody = {
  provider?: Provider;
  cookie?: string;
  targetFolder?: string;
  createFolder?: boolean;
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

function parseFolders(value: unknown, fallback: string) {
  try {
    const parsed = typeof value === "string" ? JSON.parse(value) : value;
    if (Array.isArray(parsed)) {
      const folders = parsed.filter((item): item is string => typeof item === "string" && item.trim().length > 0);
      if (folders.length) return folders;
    }
  } catch {
    // Use the current target folder when old data is not valid JSON.
  }
  return [fallback];
}

function publicConfig(row?: ConfigRow) {
  if (!row) {
    return {
      connected: false,
      cookieHint: "",
      targetFolder: "/影视资源库",
      folders: ["/影视资源库"],
      createFolder: true,
    };
  }

  return {
    connected: Boolean(row.encrypted_cookie),
    cookieHint: row.cookie_hint,
    targetFolder: row.target_folder,
    folders: parseFolders(row.folder_options, row.target_folder),
    createFolder: Boolean(row.create_folder),
    updatedAt: row.updated_at,
  };
}

function errorMessage(error: unknown) {
  if (error instanceof Error && error.message.trim()) return error.message;
  return "保存网盘配置失败";
}

export async function GET() {
  try {
    const configs = await rows<ConfigRow>("SELECT * FROM provider_configs");
    const map = Object.fromEntries(configs.map((row) => [row.provider, publicConfig(row)]));
    return NextResponse.json({
      baidu: map.baidu || publicConfig(),
      quark: map.quark || publicConfig(),
    });
  } catch (error) {
    console.error("[providers/config][GET]", error);
    return NextResponse.json({ error: errorMessage(error) }, { status: 500 });
  }
}

export async function POST(request: NextRequest) {
  try {
    let body: SaveBody;
    try {
      body = (await request.json()) as SaveBody;
    } catch {
      return NextResponse.json({ error: "请求内容格式不正确，请刷新页面后重试" }, { status: 400 });
    }

    if (body.provider !== "baidu" && body.provider !== "quark") {
      return NextResponse.json({ error: "网盘类型无效" }, { status: 400 });
    }

    const provider = body.provider;
    const existing = await one<ConfigRow>("SELECT * FROM provider_configs WHERE provider=$1", [provider]);
    const cookie = normalizeCookie(body.cookie || "");

    if (!cookie && !existing?.encrypted_cookie) {
      return NextResponse.json({ error: "请输入 Cookie" }, { status: 400 });
    }

    const targetFolder = normalizeFolder(body.targetFolder || existing?.target_folder || "/影视资源库");
    const oldFolders = existing ? parseFolders(existing.folder_options, existing.target_folder) : [];
    const folders = Array.from(new Set([targetFolder, ...oldFolders])).slice(0, 30);
    const encryptedCookie = cookie ? encryptSecret(cookie) : existing?.encrypted_cookie || "";
    const cookieHint = cookie ? `${cookie.slice(0, 5)}••••${cookie.slice(-4)}` : existing?.cookie_hint || "";

    await execute(
      `
        INSERT INTO provider_configs (
          provider, encrypted_cookie, cookie_hint, target_folder,
          folder_options, create_folder, updated_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, NOW())
        ON CONFLICT(provider) DO UPDATE SET
          encrypted_cookie=EXCLUDED.encrypted_cookie,
          cookie_hint=EXCLUDED.cookie_hint,
          target_folder=EXCLUDED.target_folder,
          folder_options=EXCLUDED.folder_options,
          create_folder=EXCLUDED.create_folder,
          updated_at=NOW()
      `,
      [provider, encryptedCookie, cookieHint, targetFolder, JSON.stringify(folders), body.createFolder !== false],
    );

    const saved = await one<ConfigRow>("SELECT * FROM provider_configs WHERE provider=$1", [provider]);
    if (!saved) {
      throw new Error("配置写入数据库后未能读取，请重新保存");
    }

    return NextResponse.json({ ok: true, config: publicConfig(saved) });
  } catch (error) {
    console.error("[providers/config][POST]", error);
    return NextResponse.json({ error: errorMessage(error) }, { status: 500 });
  }
}
