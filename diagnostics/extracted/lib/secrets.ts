import { createCipheriv, createDecipheriv, createHash, randomBytes } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

const keyPath = process.env.CREDENTIAL_KEY_PATH ?? join(process.cwd(), "data", ".credential-key");

function getKey() {
  const configured = String(process.env.CREDENTIAL_SECRET || "").trim();
  if (configured) return createHash("sha256").update(configured).digest();

  mkdirSync(dirname(keyPath), { recursive: true });
  if (!existsSync(keyPath)) writeFileSync(keyPath, randomBytes(32), { mode: 0o600 });
  const key = readFileSync(keyPath);
  if (key.length !== 32) throw new Error("凭据加密密钥无效");
  return key;
}

function normalize(value: string) {
  return value.trim().replace(/^cookie\s*:\s*/i, "").replace(/[\r\n]+/g, "; ").replace(/;\s*;+/g, "; ");
}

export function encryptSecret(value: string) {
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", getKey(), iv);
  const encrypted = Buffer.concat([cipher.update(normalize(value), "utf8"), cipher.final()]);
  const tag = cipher.getAuthTag();
  return `${iv.toString("base64")}.${tag.toString("base64")}.${encrypted.toString("base64")}`;
}

export function decryptSecret(value: string) {
  const [ivPart, tagPart, encryptedPart] = value.split(".");
  if (!ivPart || !tagPart || !encryptedPart) throw new Error("凭据格式无效");
  const decipher = createDecipheriv("aes-256-gcm", getKey(), Buffer.from(ivPart, "base64"));
  decipher.setAuthTag(Buffer.from(tagPart, "base64"));
  const plain = Buffer.concat([decipher.update(Buffer.from(encryptedPart, "base64")), decipher.final()]).toString("utf8");
  return normalize(plain);
}
