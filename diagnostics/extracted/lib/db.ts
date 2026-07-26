import { Pool, PoolClient, QueryResultRow, types } from "pg";

// PostgreSQL BIGINT / NUMERIC are returned as strings by default. The app only
// stores small counters and IDs, so converting them to Number keeps the
// original SQLite-facing TypeScript code simple and predictable.
types.setTypeParser(20, (value) => Number(value));
types.setTypeParser(1700, (value) => Number(value));

declare global {
  var __netdiskPool: Pool | undefined;
  var __netdiskDbReady: Promise<void> | undefined;
}

function connectionString() {
  const value = String(process.env.DATABASE_URL || "").trim();
  if (!value) throw new Error("缺少 DATABASE_URL，请在 Render 中连接 Neon PostgreSQL");
  return value;
}

function pool() {
  if (!globalThis.__netdiskPool) {
    const url = connectionString();
    globalThis.__netdiskPool = new Pool({
      connectionString: url,
      ssl: /localhost|127\.0\.0\.1/i.test(url) ? false : { rejectUnauthorized: false },
      max: 5,
      idleTimeoutMillis: 30_000,
      connectionTimeoutMillis: 20_000,
      options: "-c search_path=dual_sync,public",
    });
  }
  return globalThis.__netdiskPool;
}

async function initialize() {
  const client = await pool().connect();
  try {
    await client.query("CREATE SCHEMA IF NOT EXISTS dual_sync");
    await client.query("BEGIN");
    await client.query("SET LOCAL search_path TO dual_sync, public");
    await client.query(`
      CREATE TABLE IF NOT EXISTS resources (
        id BIGSERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        title TEXT NOT NULL,
        short_title TEXT NOT NULL,
        update_note TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT '影视',
        poster_url TEXT NOT NULL DEFAULT '',
        year INTEGER,
        rating DOUBLE PRECISION,
        heat INTEGER NOT NULL DEFAULT 20,
        primary_color TEXT NOT NULL DEFAULT '#b74335',
        secondary_color TEXT NOT NULL DEFAULT '#f0b65b',
        baidu_url TEXT NOT NULL DEFAULT '',
        quark_url TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL DEFAULT '百度网盘',
        status TEXT NOT NULL DEFAULT '持续更新',
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        summary TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
      )
    `);
    await client.query(`
      CREATE TABLE IF NOT EXISTS sync_sources (
        id BIGSERIAL PRIMARY KEY,
        document_name TEXT NOT NULL DEFAULT '影视资源库',
        document_url TEXT NOT NULL,
        monitor_interval INTEGER NOT NULL DEFAULT 30,
        baidu_limit INTEGER NOT NULL DEFAULT 0,
        quark_limit INTEGER NOT NULL DEFAULT 0,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        last_checked_at TIMESTAMPTZ,
        last_changed_at TIMESTAMPTZ,
        watch_error TEXT NOT NULL DEFAULT '',
        applied_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,
        pending_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,
        pending_changes JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
      )
    `);
    await client.query(`
      CREATE TABLE IF NOT EXISTS sync_items (
        id BIGSERIAL PRIMARY KEY,
        source_id BIGINT REFERENCES sync_sources(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        resource_key TEXT NOT NULL DEFAULT '',
        source_baidu_url TEXT NOT NULL DEFAULT '',
        source_quark_url TEXT NOT NULL DEFAULT '',
        target_baidu_url TEXT NOT NULL DEFAULT '',
        target_quark_url TEXT NOT NULL DEFAULT '',
        baidu_status TEXT NOT NULL DEFAULT 'waiting_auth',
        quark_status TEXT NOT NULL DEFAULT 'waiting_auth',
        source_fingerprint TEXT NOT NULL DEFAULT '',
        last_synced_at TIMESTAMPTZ,
        last_error TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(name, source_id)
      )
    `);
    await client.query("CREATE INDEX IF NOT EXISTS idx_sync_items_source ON sync_items(source_id)");
    await client.query("CREATE INDEX IF NOT EXISTS idx_sync_items_resource_key ON sync_items(source_id, resource_key)");
    await client.query(`
      CREATE TABLE IF NOT EXISTS provider_configs (
        provider TEXT PRIMARY KEY,
        encrypted_cookie TEXT NOT NULL DEFAULT '',
        cookie_hint TEXT NOT NULL DEFAULT '',
        target_folder TEXT NOT NULL DEFAULT '/影视资源库',
        folder_options TEXT NOT NULL DEFAULT '[\"/影视资源库\"]',
        create_folder BOOLEAN NOT NULL DEFAULT TRUE,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
      )
    `);
    await client.query(`
      CREATE TABLE IF NOT EXISTS provider_fingerprints (
        item_id BIGINT NOT NULL REFERENCES sync_items(id) ON DELETE CASCADE,
        provider TEXT NOT NULL,
        fingerprint TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (item_id, provider)
      )
    `);
    await client.query(`
      CREATE TABLE IF NOT EXISTS provider_assets (
        item_id BIGINT NOT NULL REFERENCES sync_items(id) ON DELETE CASCADE,
        provider TEXT NOT NULL,
        remote_ids TEXT NOT NULL DEFAULT '[]',
        remote_paths TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (item_id, provider)
      )
    `);
    await client.query(`
      CREATE TABLE IF NOT EXISTS sync_jobs (
        id BIGSERIAL PRIMARY KEY,
        item_id BIGINT NOT NULL REFERENCES sync_items(id) ON DELETE CASCADE,
        provider TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        UNIQUE(item_id, provider)
      )
    `);
    await client.query("CREATE INDEX IF NOT EXISTS idx_sync_jobs_status ON sync_jobs(status, id)");
    await client.query(`
      CREATE TABLE IF NOT EXISTS document_configs (
        id INTEGER PRIMARY KEY CHECK(id = 1),
        encrypted_cookie TEXT NOT NULL DEFAULT '',
        cookie_hint TEXT NOT NULL DEFAULT '',
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
      )
    `);

    // Safe migrations for deployments that already created an earlier schema.
    await client.query("ALTER TABLE sync_sources ADD COLUMN IF NOT EXISTS baidu_limit INTEGER NOT NULL DEFAULT 0");
    await client.query("ALTER TABLE sync_sources ADD COLUMN IF NOT EXISTS quark_limit INTEGER NOT NULL DEFAULT 0");
    await client.query("ALTER TABLE sync_sources ADD COLUMN IF NOT EXISTS last_changed_at TIMESTAMPTZ");
    await client.query("ALTER TABLE sync_sources ADD COLUMN IF NOT EXISTS watch_error TEXT NOT NULL DEFAULT ''");
    await client.query("ALTER TABLE sync_sources ADD COLUMN IF NOT EXISTS applied_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb");
    await client.query("ALTER TABLE sync_sources ADD COLUMN IF NOT EXISTS pending_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb");
    await client.query("ALTER TABLE sync_sources ADD COLUMN IF NOT EXISTS pending_changes JSONB NOT NULL DEFAULT '[]'::jsonb");
    await client.query("ALTER TABLE sync_sources ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()");
    await client.query("ALTER TABLE sync_items ADD COLUMN IF NOT EXISTS resource_key TEXT NOT NULL DEFAULT ''");

    await client.query("COMMIT");
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}

export async function ensureDb() {
  if (!globalThis.__netdiskDbReady) globalThis.__netdiskDbReady = initialize();
  return globalThis.__netdiskDbReady;
}

export async function rows<T extends QueryResultRow = QueryResultRow>(text: string, values: unknown[] = []) {
  await ensureDb();
  return (await pool().query<T>(text, values)).rows;
}

export async function one<T extends QueryResultRow = QueryResultRow>(text: string, values: unknown[] = []) {
  const result = await rows<T>(text, values);
  return result[0] as T | undefined;
}

export async function execute(text: string, values: unknown[] = []) {
  await ensureDb();
  return pool().query(text, values);
}

export async function transaction<T>(fn: (client: PoolClient) => Promise<T>) {
  await ensureDb();
  const client = await pool().connect();
  try {
    await client.query("BEGIN");
    const value = await fn(client);
    await client.query("COMMIT");
    return value;
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}
