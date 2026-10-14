import { useEffect, useRef, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useSaveMcpServer, fetchMcpServer, testMcpServer } from "../../api/queries";
import { McpTestResult } from "../../components/McpTestResult";
import { useToast } from "../../components/Toast";
import { ApiError } from "../../api/client";
import { Button, Field, Note, Dropdown } from "../../ui";
import { Input } from "../../ui/inputs";
import { Icons } from "../../ui/Icon";
import { slugNameError } from "../../lib/validation";
import type { McpAuthType, McpConnectionTestResult } from "../../api/types";

type Draft = {
  id?: string;
  name: string;
  description: string;
  url: string;
  auth_type: McpAuthType;
  auth_header_name: string;
  enabled: boolean;
  // secret_set tracks whether the server currently has a secret stored
  secret_set: boolean;
};

const EMPTY: Draft = {
  name: "", description: "", url: "",
  auth_type: "none", auth_header_name: "",
  enabled: true, secret_set: false,
};

function urlValidationError(url: string): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    const hasControl = Array.from(url).some((char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127);
    if (["https:", "http:"].includes(parsed.protocol) && parsed.hostname &&
      /^https?:\/\//i.test(url) && !url.includes("\\") &&
      !parsed.username && !parsed.password && !parsed.hash && !/\s/.test(url) && !hasControl) return null;
  } catch { /* show field error */ }
  return "URL must be a valid http:// or https:// endpoint";
}

export default function McpEditor() {
  const { id } = useParams<{ id?: string }>();
  const navigate = useNavigate();
  const toast = useToast();
  const save = useSaveMcpServer();

  const [draft, setDraft] = useState<Draft | null>(id ? null : { ...EMPTY });
  const [loadError, setLoadError] = useState(false);
  // secret is kept separate from Draft — it's write-only, never pre-filled
  const [secret, setSecret] = useState("");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<McpConnectionTestResult | null>(null);
  const testGeneration = useRef(0);

  useEffect(() => {
    setTestResult(null);
    testGeneration.current += 1;
    return () => { testGeneration.current += 1; };
  }, [draft?.id, draft?.url, draft?.auth_type, draft?.auth_header_name, draft?.secret_set, secret]);

  // Edit: fetch the MCP server once to pre-fill non-secret fields
  useEffect(() => {
    setSecret("");
    setLoadError(false);
    if (!id) {
      setDraft({ ...EMPTY });
      return;
    }
    setDraft(null);
    let alive = true;
    fetchMcpServer(id)
      .then((s) => {
        if (!alive) return;
        setDraft({
          id: s.id,
          name: s.name,
          description: s.description,
          url: s.url,
          auth_type: s.auth_type,
          auth_header_name: s.auth_header_name ?? "",
          enabled: s.enabled,
          secret_set: s.secret_set,
        });
      })
      .catch(() => { if (alive) setLoadError(true); });
    return () => { alive = false; };
  }, [id]);

  function back() { navigate("/settings/mcp"); }

  if (id && loadError) {
    return (
      <div className="page" style={{ maxWidth: 720 }}>
        <Note tone="amber">
          Couldn't load this MCP server. It may have been deleted.{" "}
          <button className="btn btn-ghost btn-sm" onClick={back}>Back to MCP servers</button>
        </Note>
      </div>
    );
  }

  if (!draft) {
    return (
      <div className="page" style={{ maxWidth: 720 }}>
        <div className="skel" style={{ width: 180, height: 16, marginBottom: 12 }} />
        <div className="card" style={{ display: "grid", gap: 12 }}>
          <div className="skel" style={{ width: "60%", height: 12 }} />
          <div className="skel" style={{ width: "90%", height: 12 }} />
          <div className="skel" style={{ width: "40%", height: 12 }} />
        </div>
      </div>
    );
  }

  const isEdit = !!draft.id;
  const invalidName = slugNameError(draft.name, "my-server");
  const invalidUrl = urlValidationError(draft.url);
  const needsSecret = draft.auth_type !== "none";
  // On create, secret is required when auth_type !== none; on edit it can be omitted to keep existing
  const secretMissing = needsSecret && !secret.trim() && !draft.secret_set;
  const busy = save.isPending || testing;
  const canTest = !!draft.url && !invalidUrl && !secretMissing &&
    (draft.auth_type !== "header" || !!draft.auth_header_name.trim());
  const canSave =
    !!draft.name && !invalidName &&
    !!draft.url && !invalidUrl &&
    (draft.auth_type !== "header" || !!draft.auth_header_name.trim()) &&
    !secretMissing;

  async function onSave() {
    if (!draft || !canSave) return;
    try {
      // Build payload — secret is write-only: omit if empty (keep existing on edit); send if non-empty
      const secretPayload: { secret?: string } = secret.trim() ? { secret: secret.trim() } : {};

      await save.mutateAsync({
        id: draft.id,
        name: draft.name,
        description: draft.description,
        url: draft.url,
        auth_type: draft.auth_type,
        auth_header_name: draft.auth_type === "header" ? draft.auth_header_name || null : null,
        enabled: draft.enabled,
        ...secretPayload,
      });
      toast.success(isEdit ? "MCP server updated" : "MCP server created");
      navigate("/settings/mcp");
    } catch (err) {
      toast.error("Couldn't save MCP server", err instanceof ApiError ? err.message : undefined);
    }
  }

  async function onTest() {
    if (!draft || !canTest || busy) return;
    const generation = ++testGeneration.current;
    setTesting(true);
    setTestResult(null);
    try {
      const result = await testMcpServer({
        id: draft.id, url: draft.url, auth_type: draft.auth_type,
        auth_header_name: draft.auth_type === "header" ? draft.auth_header_name.trim() : null,
        ...(secret.trim() ? { secret: secret.trim() } : {}),
      });
      if (generation === testGeneration.current) setTestResult(result);
    } catch (err) {
      if (generation === testGeneration.current) {
        toast.error("连接测试失败", err instanceof ApiError ? err.message : undefined);
      }
    } finally {
      setTesting(false);
    }
  }

  async function onClearSecret() {
    if (!draft?.id) return;
    try {
      await save.mutateAsync({
        id: draft.id,
        name: draft.name,
        description: draft.description,
        url: draft.url,
        auth_type: draft.auth_type,
        auth_header_name: draft.auth_type === "header" ? draft.auth_header_name || null : null,
        enabled: draft.enabled,
        secret: "",   // explicit empty string = clear
      });
      setDraft((d) => d ? { ...d, secret_set: false } : d);
      toast.success("Secret cleared");
    } catch (err) {
      toast.error("Couldn't clear secret", err instanceof ApiError ? err.message : undefined);
    }
  }

  const saveLabel = save.isPending ? (isEdit ? "Saving…" : "Creating…") : (isEdit ? "Save server" : "Create server");

  const secretPlaceholder = isEdit
    ? (draft.secret_set ? "•••••••• (configured — leave blank to keep)" : "enter a token")
    : "enter a token";

  return (
    <div
      className="responsive-editor"
      style={{
        height: "100%", overflow: "hidden", background: "var(--surface)",
        display: "flex", flexDirection: "column",
      }}
    >
      <div
        style={{
          flex: 1, overflow: "auto", padding: "22px 24px 28px",
          display: "flex", flexDirection: "column", gap: 18, maxWidth: 720,
        }}
      >
        {/* Header */}
        <div>
          <button
            className="btn btn-ghost btn-sm"
            onClick={back}
            style={{ gap: 6, padding: "4px 8px 4px 4px", marginBottom: 8, marginLeft: -4 }}
          >
            <Icons.ArrowLeft w={15} /> MCP servers
          </button>
          <div style={{ fontSize: 22, fontWeight: 800, letterSpacing: "-0.02em" }}>
            {isEdit ? "Edit MCP server" : "New MCP server"}
          </div>
          <div style={{ fontSize: 13, color: "var(--muted)", marginTop: 4 }}>
            Connect an external MCP server so agents can call its tools.
          </div>
        </div>

        {/* Name */}
        <Field label="Name" hint="Identifier for this server. Cannot be changed after creation." htmlFor="mcp-name">
          <Input
            id="mcp-name"
            className="fluid-w"
            aria-label="Name"
            value={draft.name}
            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            placeholder="linear-mcp"
            disabled={isEdit || busy}
            aria-invalid={!!invalidName}
          />
          {invalidName && <span className="hint" style={{ color: "var(--err-700)" }}>{invalidName}</span>}
        </Field>

        {/* Description */}
        <Field label="Description" hint="What this server provides." htmlFor="mcp-desc">
          <Input
            id="mcp-desc"
            className="fluid-w"
            aria-label="Description"
            disabled={busy}
            value={draft.description}
            onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            placeholder="Linear project management tools"
          />
        </Field>

        {/* URL */}
        <Field label="URL" hint="The HTTP or HTTPS endpoint for the MCP server." htmlFor="mcp-url">
          <Input
            id="mcp-url"
            className="fluid-w"
            aria-label="URL"
            disabled={busy}
            value={draft.url}
            onChange={(e) => setDraft({ ...draft, url: e.target.value })}
            placeholder="https://mcp.example.com/mcp"
            aria-invalid={!!invalidUrl}
          />
          {invalidUrl && <span className="hint" style={{ color: "var(--err-700)" }}>{invalidUrl}</span>}
        </Field>

        {/* Auth type */}
        <Field label="Authentication" htmlFor="mcp-auth">
          <Dropdown
            id="mcp-auth"
            aria-label="Authentication type"
            disabled={busy}
            value={draft.auth_type}
            onChange={(v) => setDraft({ ...draft, auth_type: v as McpAuthType })}
            options={[
              { value: "none", label: "None" },
              { value: "bearer", label: "Bearer token" },
              { value: "header", label: "Custom header" },
            ]}
          />
        </Field>

        {/* Auth header name — only when auth_type === "header" */}
        {draft.auth_type === "header" && (
          <Field label="Header name" hint='The HTTP header name to send the secret in (e.g. "X-Api-Key").' htmlFor="mcp-header-name">
            <Input
              id="mcp-header-name"
              className="fluid-w"
              aria-label="Header name"
              disabled={busy}
              value={draft.auth_header_name}
              onChange={(e) => setDraft({ ...draft, auth_header_name: e.target.value })}
              placeholder="X-Api-Key"
            />
          </Field>
        )}

        {/* Secret — only when auth_type !== "none" */}
        {needsSecret && (
          <Field
            label="Secret"
            hint={isEdit ? "Write-only. Leave blank to keep the existing secret." : undefined}
            htmlFor="mcp-secret"
          >
            <div style={{ display: "flex", gap: 8, alignItems: "stretch" }}>
              <Input
                id="mcp-secret"
                type="password"
                className="fluid-w"
                aria-label="Secret"
                disabled={busy}
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
                placeholder={secretPlaceholder}
                style={{ flex: 1 }}
              />
              {isEdit && draft.secret_set && (
                <Button
                  variant="danger"
                  onClick={onClearSecret}
                  disabled={busy}
                  style={{ whiteSpace: "nowrap", flexShrink: 0, fontSize: 12.5, padding: "0 14px" }}
                >
                  Clear secret
                </Button>
              )}
            </div>
          </Field>
        )}
        {testResult && <McpTestResult result={testResult} />}
      </div>

      {/* Footer actions */}
      <div style={{
        display: "flex", justifyContent: "flex-end", gap: 8,
        padding: "12px 24px", borderTop: "1px solid var(--border)",
        background: "var(--surface)",
      }}>
        <Button variant="secondary" size="md" onClick={back}>Cancel</Button>
        <Button variant="secondary" size="md" onClick={onTest} disabled={!canTest || busy}>
          {testing ? "测试中…" : "测试连接"}
        </Button>
        <Button variant="primary" size="md" onClick={onSave} disabled={!canSave || busy}>{saveLabel}</Button>
      </div>
    </div>
  );
}
