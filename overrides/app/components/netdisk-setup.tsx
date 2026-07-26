"use client";

import { useEffect, useState } from "react";

type Provider = "baidu" | "quark";
type ProviderConfig = {
  connected: boolean;
  cookieHint: string;
  targetFolder: string;
  folders: string[];
  createFolder: boolean;
};

type SaveResult = {
  ok?: boolean;
  error?: string;
};

async function readJsonResponse(response: Response): Promise<SaveResult> {
  const text = await response.text();
  if (!text.trim()) return {};

  try {
    return JSON.parse(text) as SaveResult;
  } catch {
    return { error: `服务返回了无法识别的内容（HTTP ${response.status}）` };
  }
}

export function NetdiskSetup({
  provider,
  name,
  config,
  onSaved,
}: {
  provider: Provider;
  name: string;
  config: ProviderConfig;
  onSaved: () => void;
}) {
  const [cookie, setCookie] = useState("");
  const [folder, setFolder] = useState(config.targetFolder);
  const [creatingNew, setCreatingNew] = useState(false);
  const [newFolder, setNewFolder] = useState("");
  const [createFolder, setCreateFolder] = useState(config.createFolder);
  const [saving, setSaving] = useState(false);
  const [note, setNote] = useState("");

  useEffect(() => {
    setFolder(config.targetFolder);
    setCreateFolder(config.createFolder);
    setCreatingNew(false);
  }, [config.targetFolder, config.createFolder]);

  const save = async () => {
    setSaving(true);
    setNote("");

    try {
      const targetFolder = creatingNew ? newFolder : folder;
      const response = await fetch("/api/providers/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, cookie, targetFolder, createFolder }),
      });

      const result = await readJsonResponse(response);
      if (!response.ok) {
        throw new Error(result.error || `保存失败（HTTP ${response.status}）`);
      }
      if (!result.ok) {
        throw new Error(result.error || "保存接口未返回成功结果，请重新保存");
      }

      setCookie("");
      setNewFolder("");
      setCreatingNew(false);
      setNote("Cookie 和转存目录已安全保存");
      onSaved();
    } catch (error) {
      setNote(error instanceof Error ? error.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="panel">
      <div className="section-title">
        <span className="step">{provider === "baidu" ? "1" : "2"}</span>
        <div>
          <h2>{name}配置</h2>
          <p>Cookie 加密保存，不会在页面中回显</p>
        </div>
      </div>
      <div className="mt-5 space-y-4">
        <label className="field">
          <span>
            Cookie {config.connected && <em>已保存：{config.cookieHint}</em>}
          </span>
          <textarea
            rows={3}
            value={cookie}
            onChange={(event) => setCookie(event.target.value)}
            placeholder={config.connected ? "留空表示继续使用已保存的 Cookie" : `粘贴${name}网页版完整 Cookie`}
            spellCheck={false}
            autoComplete="off"
          />
        </label>
        <label className="field">
          <span>转存文件夹</span>
          <select
            value={creatingNew ? "__new__" : folder}
            onChange={(event) => {
              const isNew = event.target.value === "__new__";
              setCreatingNew(isNew);
              if (!isNew) setFolder(event.target.value);
            }}
          >
            {config.folders.map((item) => (
              <option key={item}>{item}</option>
            ))}
            <option value="__new__">＋ 创建新文件夹</option>
          </select>
        </label>
        {creatingNew && (
          <label className="field">
            <span>新文件夹路径</span>
            <input
              value={newFolder}
              placeholder="/影视资源库/电视剧"
              onChange={(event) => setNewFolder(event.target.value)}
            />
          </label>
        )}
        <label className="toggle-line">
          <input
            type="checkbox"
            checked={createFolder}
            onChange={(event) => setCreateFolder(event.target.checked)}
          />
          <span>文件夹不存在时自动创建</span>
        </label>
        <button
          className="secondary-button w-full"
          onClick={save}
          disabled={saving || (creatingNew && !newFolder.trim())}
        >
          {saving ? "保存中…" : `保存${name}配置`}
        </button>
        {note && <p className="config-note">{note}</p>}
      </div>
    </div>
  );
}
