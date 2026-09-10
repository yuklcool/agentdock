import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../../api/client";
import {
  discoverSkillArchive,
  importSkillArchive,
  type SkillArchiveDiscoverResponse,
} from "../../api/skillArchives";
import { useToast } from "../../components/Toast";
import { Button } from "../../ui/Button";
import { Icons } from "../../ui/Icon";

const ACCEPTED = [".zip", ".tar.gz", ".tgz"];

function acceptedArchive(file: File) {
  const name = file.name.toLowerCase();
  return ACCEPTED.some((suffix) => name.endsWith(suffix));
}

function prettyBytes(value: number | null) {
  if (value == null) return "";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export function SkillArchiveImportDialog({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const queryClient = useQueryClient();
  const toast = useToast();
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<SkillArchiveDiscoverResponse | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectable = useMemo(
    () => preview?.skills.filter((skill) => skill.valid && !skill.installed) ?? [],
    [preview],
  );

  function reset() {
    setFile(null);
    setPreview(null);
    setSelected(new Set());
    setBusy(false);
    setDragging(false);
    setError(null);
    if (inputRef.current) inputRef.current.value = "";
  }

  function close() {
    if (busy) return;
    reset();
    onClose();
  }

  useEffect(() => {
    if (!open) reset();
  }, [open]);

  async function inspect(next: File) {
    if (!acceptedArchive(next)) {
      setFile(null);
      setPreview(null);
      setSelected(new Set());
      setError("请选择 .zip、.tar.gz 或 .tgz 技能包。");
      return;
    }
    setFile(next);
    setPreview(null);
    setSelected(new Set());
    setError(null);
    setBusy(true);
    try {
      const result = await discoverSkillArchive(next);
      setPreview(result);
      setSelected(
        new Set(
          result.skills
            .filter((skill) => skill.valid && !skill.installed)
            .map((skill) => skill.subpath),
        ),
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "技能包解析失败。");
    } finally {
      setBusy(false);
    }
  }

  async function doImport() {
    if (!file || selected.size === 0) return;
    setBusy(true);
    setError(null);
    try {
      const result = await importSkillArchive(file, [...selected]);
      await queryClient.invalidateQueries({ queryKey: ["skills"] });
      toast.success(`已导入 ${result.imported.length} 个技能`);
      reset();
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "技能包导入失败。");
    } finally {
      setBusy(false);
    }
  }

  if (!open) return null;

  return (
    <div
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) close();
      }}
      style={{
        position: "fixed", inset: 0, zIndex: 1000,
        background: "rgba(9, 12, 18, 0.58)", backdropFilter: "blur(4px)",
        display: "grid", placeItems: "center", padding: 20,
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="skill-archive-title"
        style={{
          width: "min(760px, 100%)", maxHeight: "min(760px, calc(100vh - 40px))",
          overflow: "auto", borderRadius: 16, border: "1px solid var(--border)",
          background: "var(--surface-1)", boxShadow: "0 24px 70px rgba(0,0,0,.28)",
        }}
      >
        <div style={{ padding: "20px 22px 14px", display: "flex", alignItems: "flex-start", gap: 16 }}>
          <div style={{ flex: 1 }}>
            <div id="skill-archive-title" style={{ fontSize: 18, fontWeight: 750 }}>上传技能包</div>
            <div style={{ color: "var(--muted)", fontSize: 12.5, marginTop: 5 }}>
              支持 ZIP、TAR.GZ 和 TGZ。上传后先检测 SKILL.md，确认后再批量导入。
            </div>
          </div>
          <Button variant="secondary" size="sm" onClick={close} disabled={busy} aria-label="关闭">
            <Icons.Close w={14} />
          </Button>
        </div>

        <div style={{ padding: "0 22px 22px", display: "grid", gap: 14 }}>
          <input
            ref={inputRef}
            type="file"
            accept=".zip,.tar.gz,.tgz,application/zip,application/gzip"
            style={{ display: "none" }}
            onChange={(event) => {
              const next = event.target.files?.[0];
              if (next) void inspect(next);
            }}
          />

          <button
            type="button"
            disabled={busy}
            onClick={() => inputRef.current?.click()}
            onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
            onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
            onDragLeave={(event) => { event.preventDefault(); setDragging(false); }}
            onDrop={(event) => {
              event.preventDefault();
              setDragging(false);
              const next = event.dataTransfer.files?.[0];
              if (next) void inspect(next);
            }}
            style={{
              width: "100%", minHeight: 142, borderRadius: 13,
              border: `1.5px dashed ${dragging ? "var(--p-600)" : "var(--border)"}`,
              background: dragging ? "var(--surface-3)" : "var(--surface-2)",
              color: "var(--ink)", cursor: busy ? "wait" : "pointer",
              display: "grid", placeItems: "center", padding: 20,
            }}
          >
            <span style={{ display: "grid", placeItems: "center", gap: 7 }}>
              <span style={{ width: 38, height: 38, borderRadius: 10, background: "var(--p-300)", display: "grid", placeItems: "center" }}>
                <Icons.File w={19} />
              </span>
              <strong style={{ fontSize: 13.5 }}>{busy ? "正在检测技能包…" : file ? file.name : "拖拽技能包到这里，或点击选择文件"}</strong>
              <span style={{ color: "var(--muted)", fontSize: 11.5 }}>单个技能或包含多个技能的压缩包均可</span>
            </span>
          </button>

          {error && (
            <div role="alert" style={{ padding: "10px 12px", borderRadius: 10, background: "var(--danger-bg, rgba(220,38,38,.08))", color: "var(--danger, #b42318)", fontSize: 12.5 }}>
              {error}
            </div>
          )}

          {preview && (
            <div className="card flush" style={{ overflow: "hidden" }}>
              <div style={{ padding: "11px 13px", borderBottom: "1px solid var(--border)", display: "flex", alignItems: "center", gap: 10 }}>
                <strong style={{ fontSize: 13 }}>检测到 {preview.skills.length} 个技能</strong>
                <span style={{ color: "var(--muted)", fontSize: 11.5 }}>
                  {selected.size} 个待导入
                </span>
                {selectable.length > 0 && (
                  <button
                    type="button"
                    className="btn btn-secondary btn-sm"
                    style={{ marginLeft: "auto" }}
                    onClick={() => {
                      const allSelected = selectable.every((skill) => selected.has(skill.subpath));
                      setSelected(allSelected ? new Set() : new Set(selectable.map((skill) => skill.subpath)));
                    }}
                  >
                    {selectable.every((skill) => selected.has(skill.subpath)) ? "取消全选" : "全选可导入"}
                  </button>
                )}
              </div>

              {preview.truncated && (
                <div style={{ padding: "9px 13px", background: "var(--surface-3)", fontSize: 11.5, color: "var(--muted)" }}>
                  技能数量超过扫描上限，仅展示前 50 个技能。
                </div>
              )}

              <div style={{ maxHeight: 300, overflow: "auto" }}>
                {preview.skills.map((skill) => {
                  const disabled = !skill.valid || skill.installed;
                  return (
                    <label
                      key={`${skill.subpath}:${skill.name}`}
                      style={{
                        display: "grid", gridTemplateColumns: "20px 1fr auto", gap: 10,
                        alignItems: "start", padding: "11px 13px",
                        borderBottom: "1px solid var(--border)",
                        opacity: disabled ? 0.68 : 1,
                        cursor: disabled ? "default" : "pointer",
                      }}
                    >
                      <input
                        type="checkbox"
                        disabled={disabled || busy}
                        checked={!disabled && selected.has(skill.subpath)}
                        onChange={(event) => {
                          setSelected((current) => {
                            const next = new Set(current);
                            if (event.target.checked) next.add(skill.subpath);
                            else next.delete(skill.subpath);
                            return next;
                          });
                        }}
                        style={{ marginTop: 2 }}
                      />
                      <span style={{ minWidth: 0 }}>
                        <span style={{ display: "flex", alignItems: "center", gap: 7, flexWrap: "wrap" }}>
                          <strong style={{ fontSize: 13 }}>{skill.name}</strong>
                          {skill.installed && <span className="tag">已安装</span>}
                          {!skill.valid && <span className="tag">无效</span>}
                        </span>
                        {skill.description && (
                          <span style={{ display: "block", color: "var(--muted)", fontSize: 11.5, marginTop: 2 }}>
                            {skill.description}
                          </span>
                        )}
                        {skill.error && (
                          <span style={{ display: "block", color: "var(--danger, #b42318)", fontSize: 11.5, marginTop: 3 }}>
                            {skill.error}
                          </span>
                        )}
                        <span style={{ display: "block", color: "var(--muted)", fontSize: 10.5, marginTop: 3, fontFamily: "var(--font-mono)" }}>
                          {skill.subpath || "/"}
                        </span>
                      </span>
                      <span style={{ color: "var(--muted)", fontSize: 10.5, whiteSpace: "nowrap" }}>
                        {prettyBytes(skill.bundle_size)}
                      </span>
                    </label>
                  );
                })}
              </div>
            </div>
          )}

          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <Button variant="secondary" onClick={close} disabled={busy}>取消</Button>
            <Button onClick={() => void doImport()} disabled={busy || !file || selected.size === 0}>
              {busy ? "处理中…" : `导入 ${selected.size} 个技能`}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
