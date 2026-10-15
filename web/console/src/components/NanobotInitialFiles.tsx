import { useState } from "react";
import { Button, Field, Note } from "../ui";
import type { NanobotFiles } from "../api/types";
export type { NanobotFiles } from "../api/types";

export const NANOBOT_FILES = ["config.json", "AGENTS.md", "SOUL.md", "USER.md", "HEARTBEAT.md", "TOOLS.md"] as const;

export function nanobotFilesError(files: NanobotFiles): string | null {
  for (const [name, text] of Object.entries(files)) {
    if (text == null) continue;
    if (new TextEncoder().encode(text).length > 65536) return `${name} 不能超过 64 KiB`;
    if (name === "config.json") {
      try {
        const value: unknown = JSON.parse(text);
        if (!value || typeof value !== "object" || Array.isArray(value)) return "config.json 必须是 JSON 对象";
      } catch { return "config.json 格式无效，请修正后保存"; }
    }
  }
  return null;
}

export function NanobotInitialFiles({ value, onChange }: { value: NanobotFiles; onChange: (files: NanobotFiles) => void }) {
  const [active, setActive] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const error = uploadError ?? nanobotFilesError(value);
  function setFile(name: typeof NANOBOT_FILES[number], text: string | null) {
    const next = { ...value };
    if (text === null) delete next[name]; else next[name] = text;
    onChange(next);
    setUploadError(null);
  }
  return <section className="section-card">
    <div className="section-card-head"><span className="section-card-title">Nanobot 初始化文件</span></div>
    <div className="section-card-body" style={{ display: "grid", gap: 14 }}>
      <Note>未自定义的文件由当前 Nanobot 版本原生初始化，平台不保存默认副本。修改仅影响以后新建的容器；恢复默认只删除模板覆盖。</Note>
      <Note>Model、Provider、Credential 和 Base URL 由 AgentDock 管理，config.json 不会覆盖容器运行时选择。请勿在文件中填写 API Key；模型、镜像、资源与环境变量请在创建容器时配置。</Note>
      {NANOBOT_FILES.map((name) => {
        const custom = value[name] != null;
        return <div key={name} style={{ borderBottom: "1px solid var(--border)", paddingBottom: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span className="mono">{name}</span>
            <span className="tag">{custom ? "自定义 Override" : "使用 Nanobot 默认"}</span>
            <Button variant="ghost" aria-label={`编辑 ${name}`} onClick={() => { if (!custom) setFile(name, name === "config.json" ? "{}" : ""); setActive(active === name ? null : name); }}>编辑</Button>
            <label className="btn btn-ghost btn-sm">
              {custom ? "重新上传" : "上传"}
              <input type="file" aria-label={`上传 ${name}`} accept={name === "config.json" ? ".json,application/json" : ".md,.txt,text/plain,text/markdown"} style={{ display: "none" }} onChange={async (event) => {
                const input = event.currentTarget;
                const file = input.files?.[0];
                if (!file) return;
                try {
                  if (file.size > 65536) throw new Error("文件不能超过 64 KiB");
                  const text = await file.text();
                  setFile(name, text);
                  setActive(name);
                } catch { setUploadError("上传失败：请使用不超过 64 KiB 的 UTF-8 文本文件"); }
                input.value = "";
              }} />
            </label>
            {custom && <Button variant="ghost" aria-label={`恢复 ${name} 为 Nanobot 默认`} onClick={() => { setFile(name, null); if (active === name) setActive(null); }}>恢复为 Nanobot 默认</Button>}
          </div>
          {active === name && custom && <Field label={`${name} 内容`} htmlFor={`init-${name}`}>
            <textarea id={`init-${name}`} className="input fluid-w mono" rows={12} value={value[name] ?? ""} onChange={(event) => setFile(name, event.target.value)} />
            {name === "config.json" && <Button variant="ghost" onClick={() => { try { setFile(name, JSON.stringify(JSON.parse(value[name] ?? ""), null, 2)); } catch { setUploadError("config.json 格式无效，请修正后格式化"); } }}>格式化 JSON</Button>}
          </Field>}
        </div>;
      })}
      {error && <div role="alert">{error}</div>}
    </div>
  </section>;
}
