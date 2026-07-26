import { createCipheriv, createDecipheriv, createHash, randomBytes } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

const legacyKeyPath = process.env.CREDENTIAL_KEY_PATH ?? join(process.cwd(), "data", ".credential-key");

function normalize(value: string) {
  return value
    .trim()
    .replace(/^cookie\s*:\s*/i, "")
    .replace(/[\r\n]+/g, "; ")
    .replace(/;\s*;+/g, "; ");
}

function primaryKey() {
  const configured = String(process.env.CREDENTIAL_SECRET || "").trim();
  if (configured) return createHash("sha256").update(configured).digest();

  // Render's local filesystem is ephemeral. Deriving the fallback key from the
  // Neon connection secret keeps encrypted cookies readable after redeploys.
  const databaseUrl = String(process.env.DATABASE_URL || "").trim();
  if (databaseUrl) {
    return createHash("sha256").update(`netdisk-auto-sync:${databaseUrl}`).digest();
  }

  const serviceIdentity = String(
    process.env.RENDER_SERVICE_ID || process.env.RENDER_EXTERNAL_URL || "netdisk-auto-sync-local",
  ).trim();
  return createHash("sha256").update(`netdisk-auto-sync:${serviceIdentity}`).digest();
}

function legacyKey() {
  try {
    if (!existsSync(legacyKeyPath)) return null;
    const key = readFileSync(legacyKeyPath);
    return key.length === 32 ? key : null;
  } catch {
    return null;
  }
}

function decryptWithKey(value: string, key: Buffer) {
  const [ivPart, tagPart, encryptedPart] = value.split(".");
  if (!ivPart || !tagPart || !encryptedPart) throw new Error("凭据格式无效");

  const decipher = createDecipheriv("aes-256-gcm", key, Buffer.from(ivPart, "base64"));
  decipher.setAuthTag(Buffer.from(tagPart, "base64"));
  return Buffer.concat([
    decipher.update(Buffer.from(encryptedPart, "base64")),
    decipher.final(),
  ]).toString("utf8");
}

export function encryptSecret(value: string) {
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", primaryKey(), iv);
  const encrypted = Buffer.concat([cipher.update(normalize(value), "utf8"), cipher.final()]);
  const tag = cipher.getAuthTag();
  return `${iv.toString("base64")}.${tag.toString("base64")}.${encrypted.toString("base64")}`;
}

export function decryptSecret(value: string) {
  const keys = [primaryKey(), legacyKey()].filter((key): key is Buffer => Boolean(key));
  let lastError: unknown;

  for (const key of keys) {
    try {
      return normalize(decryptWithKey(value, key));
    } catch (error) {
      lastError = error;
    }
  }

  if (lastError instanceof Error && lastError.message === "凭据格式无效") {
    throw lastError;
  }
  throw new Error("凭据无法解密，请重新填写 Cookie 并保存");
}
