import { useState, useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useCredentials, useSetCredential, useUpdateCredentialEndpoint, useStartOpenAIOAuth, fetchOAuthConnection, keys, useStartAnthropicOAuth, useCompleteAnthropicOAuth, useCredentialProviders } from "../../api/queries";
import { api, ApiError } from "../../api/client";
import { useToast } from "../../components/Toast";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Button } from "../../ui/Button";
import { Pill } from "../../ui/Pill";
import { Avatar } from "../../ui/Avatar";
import { Field } from "../../ui/Field";
import { Input } from "../../ui/inputs";
import { Dropdown } from "../../ui/Dropdown";
import { Icons } from "../../ui/Icon";
import { EmptyRow } from "../../ui/EmptyState";
import { formatDate } from "../../lib/format";
import type { Credential } from "../../api/types";

export default function Credentials() {
  const { data } = useCredentials();
  const setCred = useSetCredential();
  const updateEndpoint = useUpdateCredentialEndpoint();
  const qc = useQueryClient();
  const toast = useToast();
  const [provider, setProvider] = useState("anthropic");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [editing, setEditing] = useState<Credential | null>(null);
  const [removing, setRemoving] = useState<Credential | null>(null);
  const [addingKey, setAddingKey] = useState(false);
  const creds = data?.credentials ?? [];
  const supportsBaseUrl = ["openai", "anthropic"].includes(provider);

  function selectProvider(value: string) {
    setProvider(value);
    setBaseUrl(creds.find((c) => c.provider === value && c.auth_method !== "oauth_subscription")?.base_url ?? "");
  }

  function editEndpoint(c: Credential) {
    setEditing(c);
    setProvider(c.provider);
    setBaseUrl(c.base_url ?? "");
    setApiKey("");
    setAddingKey(true);
    closeChatGPT();
    closeClaude();
  }

  // Provider dropdown is driven by the model catalog (api-key providers), not a
  // hardcoded list. Falls back to Anthropic until the request resolves.
  const { data: provData } = useCredentialProviders();
  const providerOptions = (provData?.providers ?? [{ id: "anthropic", label: "Anthropic" }]).map(
    (p) => ({ value: p.id, label: p.label }),
  );
  // Keep the selected provider valid as the catalog-driven list loads/changes.
  useEffect(() => {
    if (providerOptions.length && !providerOptions.some((o) => o.value === provider)) {
      setProvider(providerOptions[0].value);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provData]);

  const startOAuth = useStartOpenAIOAuth();
  const [chatgptOpen, setChatgptOpen] = useState(false);
  const [conn, setConn] = useState<{ id: string; userCode: string; uri: string } | null>(null);
  const [connStatus, setConnStatus] = useState<string>("pending");

  function closeChatGPT() {
    setChatgptOpen(false);
    setConn(null);
    setConnStatus("pending");
  }

  const startClaude = useStartAnthropicOAuth();
  const completeClaude = useCompleteAnthropicOAuth();
  const [claudeOpen, setClaudeOpen] = useState(false);
  const [claudeUrl, setClaudeUrl] = useState<string | null>(null);
  const [claudeConnId, setClaudeConnId] = useState<string | null>(null);
  const [claudeCode, setClaudeCode] = useState("");

  function closeClaude() {
    setClaudeOpen(false);
    setClaudeUrl(null);
    setClaudeConnId(null);
    setClaudeCode("");
  }

  async function onConnectClaude() {
    setAddingKey(false);
    closeChatGPT();
    closeClaude();
    setClaudeOpen(true);
    try {
      const r = await startClaude.mutateAsync();
      setClaudeConnId(r.connection_id);
      setClaudeUrl(r.authorize_url);
    } catch (err) {
      toast.error("Couldn't start Claude connection", err instanceof ApiError ? err.message : undefined);
      setClaudeOpen(false);
    }
  }

  async function onSubmitClaudeCode() {
    if (!claudeConnId || !claudeCode.trim()) return;
    try {
      const r = await completeClaude.mutateAsync({ connection_id: claudeConnId, code: claudeCode.trim() });
      if (r.status === "connected") {
        toast.success("Claude Code connected");
        qc.invalidateQueries({ queryKey: keys.credentials });
        closeClaude();
      } else {
        toast.error("Claude connection failed", r.error ?? undefined);
      }
    } catch (err) {
      toast.error("Couldn't complete Claude connection", err instanceof ApiError ? err.message : undefined);
    }
  }

  async function onConnectChatGPT() {
    setAddingKey(false);
    closeClaude();
    setChatgptOpen(true);
    setConn(null);
    setConnStatus("pending");
    try {
      const r = await startOAuth.mutateAsync();
      setConn({ id: r.connection_id, userCode: r.user_code, uri: r.verification_uri_complete ?? r.verification_uri ?? "" });
      setConnStatus("pending");
    } catch (err) {
      toast.error("Couldn't start ChatGPT connection", err instanceof ApiError ? err.message : undefined);
      setChatgptOpen(false);
    }
  }

  useEffect(() => {
    if (!conn || connStatus !== "pending") return;
    const timer = setInterval(async () => {
      try {
        const s = await fetchOAuthConnection(conn.id);
        if (s.status !== "pending") {
          setConnStatus(s.status);
          clearInterval(timer);
          if (s.status === "connected") {
            toast.success("ChatGPT connected");
            qc.invalidateQueries({ queryKey: keys.credentials });
            closeChatGPT();
          } else {
            toast.error(`ChatGPT connection ${s.status}`);
          }
        }
      } catch { /* keep polling */ }
    }, 2500);
    return () => clearInterval(timer);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conn, connStatus]);

  async function onSave() {
    try {
      if (editing) {
        await updateEndpoint.mutateAsync({ id: editing.id, base_url: baseUrl.trim() || null });
      } else {
        await setCred.mutateAsync({ provider, api_key: apiKey, base_url: supportsBaseUrl ? baseUrl.trim() || null : null });
      }
      setApiKey("");
      setBaseUrl("");
      setEditing(null);
      setAddingKey(false);
      toast.success("Credential saved");
    } catch (err) {
      toast.error("Couldn't save credential", err instanceof ApiError ? err.message : undefined);
    }
  }

  async function onRemove(c: Credential) {
    try {
      await api.del(`/v1/credentials/${c.id}`);
      toast.success("Credential removed");
      qc.invalidateQueries({ queryKey: keys.credentials });
      setRemoving(null);
    } catch (err) {
      toast.error("Couldn't remove credential", err instanceof ApiError ? err.message : undefined);
    }
  }

  return (
    <div className="page">
      {/* Header */}
      <div className="page-title" style={{ display: "flex", alignItems: "flex-end", gap: 12 }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <h1 style={{ fontSize: 22, fontWeight: 800, letterSpacing: "-0.02em", margin: 0 }}>Credentials</h1>
            <span className="pill pill-dormant" style={{ fontWeight: 500 }}>
              {creds.length} {creds.length === 1 ? "credential" : "credentials"}
            </span>
          </div>
          <div style={{ fontSize: 13, color: "var(--muted)", marginTop: 4 }}>
            Manage provider keys and Nanobot API endpoints. Keys are stored encrypted and never returned.
          </div>
        </div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <Button
            variant="secondary"
            size="sm"
            style={{ gap: 6, padding: "6px 12px 6px 10px" }}
            onClick={() => { setEditing(null); setApiKey(""); selectProvider(provider); setAddingKey(true); closeChatGPT(); closeClaude(); }}
          >
            <Icons.Key w={14} />
            Add API key
          </Button>
          <Button
            variant="primary"
            size="sm"
            style={{ gap: 6, padding: "6px 12px 6px 10px" }}
            onClick={onConnectChatGPT}
          >
            <Icons.Sparkles w={14} />
            Connect ChatGPT
          </Button>
          <Button
            variant="primary"
            size="sm"
            style={{ gap: 6, padding: "6px 12px 6px 10px" }}
            onClick={onConnectClaude}
          >
            <Icons.Sparkles w={14} />
            Connect Claude Code
          </Button>
        </div>
      </div>

      {/* Add API key — hero detail panel */}
      {addingKey && (
        <div className="cred-hero">
          <aside className="cred-hero-aside">
            <span className="cred-hero-badge">
              <Icons.Key style={{ width: 22, height: 22 }} />
            </span>
            <div className="cred-hero-title">{editing ? "Edit Base URL" : "Add an API key"}</div>
            <p className="cred-hero-lede">
              {editing ? `Update the Nanobot endpoint for ${editing.provider}. Your saved API key is kept.` : "Connect a provider key so tasks can call the model directly."}
            </p>
            <ul className="cred-hero-points">
              <li><Icons.Check style={{ width: 14, height: 14 }} /> Encrypted at rest, never returned</li>
              <li><Icons.Check style={{ width: 14, height: 14 }} /> Secrets are never shown in full</li>
              <li><Icons.Check style={{ width: 14, height: 14 }} /> {editing ? "Applies to the next Nanobot task" : "Replaces the current key for the provider"}</li>
            </ul>
          </aside>

          <div className="cred-hero-main">
            <button
              type="button"
              className="btn btn-ghost btn-icon btn-sm cred-hero-close"
              aria-label="Close"
              onClick={() => setAddingKey(false)}
            >
              <Icons.Close w={15} />
            </button>

            <div style={{ display: "flex", flexDirection: "column", gap: 16, maxWidth: 420 }}>
              {!editing && <Field label="Provider" htmlFor="prov">
                <Dropdown
                  id="prov"
                  value={provider}
                  onChange={selectProvider}
                  options={providerOptions}
                />
              </Field>}
              {!editing && <Field label="API key" htmlFor="ak" hint="Paste the secret key from your provider dashboard.">
                <Input
                  id="ak"
                  type="password"
                  placeholder={provider === "anthropic" ? "sk-ant-…" : "sk-…"}
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                />
              </Field>}
              {supportsBaseUrl && <Field
                label="Base URL (optional)"
                htmlFor="base-url"
                hint="For Nanobot tasks using this provider in this workspace. Leave blank for the default endpoint. An instance API address override takes priority."
              >
                <Input
                  id="base-url"
                  type="url"
                  placeholder={provider === "anthropic" ? "https://api.anthropic.com" : "https://api.example.com/v1"}
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                />
              </Field>}
              <div style={{ display: "flex", gap: 8, marginTop: 4 }}>
                <Button variant="primary" disabled={(!editing && !apiKey) || setCred.isPending || updateEndpoint.isPending} onClick={onSave}>
                  <Icons.Check w={15} /> Save credential
                </Button>
                <Button variant="ghost" onClick={() => setAddingKey(false)}>
                  Cancel
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Connect ChatGPT — hero detail panel */}
      {chatgptOpen && (
        <div className="cred-hero">
          <aside className="cred-hero-aside">
            <span className="cred-hero-badge">
              <Icons.Sparkles style={{ width: 22, height: 22 }} />
            </span>
            <div className="cred-hero-title">Connect ChatGPT</div>
            <p className="cred-hero-lede">
              Run tasks on your own ChatGPT subscription instead of a metered API key.
            </p>
            <ul className="cred-hero-points">
              <li><Icons.Check style={{ width: 14, height: 14 }} /> Uses your personal ChatGPT plan</li>
              <li><Icons.Check style={{ width: 14, height: 14 }} /> Authorized via OpenAI, per their Terms</li>
              <li><Icons.Check style={{ width: 14, height: 14 }} /> Tokens stored encrypted</li>
            </ul>
          </aside>

          <div className="cred-hero-main">
            <button
              type="button"
              className="btn btn-ghost btn-icon btn-sm cred-hero-close"
              aria-label="Close"
              onClick={closeChatGPT}
            >
              <Icons.Close w={15} />
            </button>

            {conn ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 16, maxWidth: 460 }}>
                <ol className="cred-steps">
                  <li>
                    <span className="cred-step-n">1</span>
                    <div>
                      <div style={{ fontWeight: 600, fontSize: 13.5 }}>Copy your one-time device code</div>
                      <div className="cred-code">{conn.userCode}</div>
                    </div>
                  </li>
                  <li>
                    <span className="cred-step-n">2</span>
                    <div>
                      <div style={{ fontWeight: 600, fontSize: 13.5, marginBottom: 8 }}>
                        Authorize on the OpenAI page
                      </div>
                      <a href={conn.uri} target="_blank" rel="noreferrer" className="btn btn-primary" style={{ gap: 6 }}>
                        Open ChatGPT to authorize <Icons.ArrowRight w={14} />
                      </a>
                    </div>
                  </li>
                </ol>
                <span style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 12.5, color: "var(--muted)" }}>
                  <span className="cred-spin" />
                  Waiting for you to authorize…
                </span>
              </div>
            ) : (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 10, fontSize: 13.5, color: "var(--muted)" }}>
                <span className="cred-spin" />
                Starting a secure connection…
              </span>
            )}
          </div>
        </div>
      )}

      {/* Connect Claude Code — hero detail panel */}
      {claudeOpen && (
        <div className="cred-hero">
          <aside className="cred-hero-aside">
            <span className="cred-hero-badge">
              <Icons.Sparkles style={{ width: 22, height: 22 }} />
            </span>
            <div className="cred-hero-title">Connect Claude Code</div>
            <p className="cred-hero-lede">
              Run tasks on your own Claude Pro/Max subscription instead of a metered API key.
            </p>
            <ul className="cred-hero-points">
              <li><Icons.Check style={{ width: 14, height: 14 }} /> Uses your personal Claude plan</li>
              <li><Icons.Check style={{ width: 14, height: 14 }} /> Authorize on Claude, then paste the code</li>
              <li><Icons.Check style={{ width: 14, height: 14 }} /> Tokens stored encrypted</li>
            </ul>
          </aside>

          <div className="cred-hero-main">
            <button
              type="button"
              className="btn btn-ghost btn-icon btn-sm cred-hero-close"
              aria-label="Close"
              onClick={closeClaude}
            >
              <Icons.Close w={15} />
            </button>

            {claudeUrl ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 16, maxWidth: 460 }}>
                <ol className="cred-steps">
                  <li>
                    <span className="cred-step-n">1</span>
                    <div>
                      <div style={{ fontWeight: 600, fontSize: 13.5, marginBottom: 8 }}>
                        Authorize on Claude
                      </div>
                      <a href={claudeUrl} target="_blank" rel="noreferrer" className="btn btn-primary" style={{ gap: 6 }}>
                        Open Claude to authorize <Icons.ArrowRight w={14} />
                      </a>
                    </div>
                  </li>
                  <li>
                    <span className="cred-step-n">2</span>
                    <div style={{ flex: 1 }}>
                      <div style={{ fontWeight: 600, fontSize: 13.5, marginBottom: 8 }}>
                        Paste the code Claude shows you
                      </div>
                      <Input
                        id="claude-code"
                        type="text"
                        placeholder="Paste authorization code"
                        value={claudeCode}
                        onChange={(e) => setClaudeCode(e.target.value)}
                      />
                      <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
                        <Button
                          variant="primary"
                          disabled={!claudeCode.trim() || completeClaude.isPending}
                          onClick={onSubmitClaudeCode}
                        >
                          <Icons.Check w={15} /> Connect
                        </Button>
                        <Button variant="ghost" onClick={closeClaude}>Cancel</Button>
                      </div>
                    </div>
                  </li>
                </ol>
              </div>
            ) : (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 10, fontSize: 13.5, color: "var(--muted)" }}>
                <span className="cred-spin" />
                Starting a secure connection…
              </span>
            )}
          </div>
        </div>
      )}

      {/* Credentials table */}
      <div className="card flush">
        <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>Provider</th>
              <th>Method</th>
              <th>Identifier</th>
              <th>Nanobot Base URL</th>
              <th>Status</th>
              <th>Added by</th>
              <th style={{ width: 200, textAlign: "right" }} />
            </tr>
          </thead>
          <tbody>
            {creds.map((c) => {
              const isSub = c.auth_method === "oauth_subscription";
              return (
                <tr key={c.id}>
                  <td style={{ fontWeight: 600, textTransform: "capitalize" }}>{c.provider}</td>
                  <td>
                    <Pill tone="info">{isSub ? "Subscription" : "API key"}</Pill>
                  </td>
                  <td>
                    <span className="chip" style={{ fontSize: 11.5, padding: "4px 8px" }}>
                      {isSub ? `acct …${c.account_tail ?? ""}` : `…${c.last4 ?? ""}`}
                    </span>
                  </td>
                  <td>
                    <span style={{ display: "block", maxWidth: 260, overflowWrap: "anywhere", fontSize: 12.5 }}>
                      {isSub || !["openai", "anthropic"].includes(c.provider) ? "—" : c.base_url || "Provider default"}
                    </span>
                  </td>
                  <td>
                    {c.status === "reauth_required" ? (
                      <Pill tone="warn">
                        <Icons.Warn w={12} /> Reauth required
                      </Pill>
                    ) : (
                      <Pill tone="running">
                        <span className="dot" /> Connected
                      </Pill>
                    )}
                  </td>
                  <td>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <Avatar name={c.created_by} size={24} />
                      <div style={{ display: "flex", flexDirection: "column", lineHeight: 1.3 }}>
                        <span style={{ fontSize: 12.5 }}>{c.created_by}</span>
                        <span className="id" style={{ fontSize: 11 }}>{formatDate(c.created_at)}</span>
                      </div>
                    </div>
                  </td>
                  <td style={{ textAlign: "right" }}>
                    {!isSub && ["openai", "anthropic"].includes(c.provider) && (
                      <Button variant="ghost" size="sm" onClick={() => editEndpoint(c)}>
                        Edit Base URL
                      </Button>
                    )}
                    <Button variant="danger" size="sm" onClick={() => setRemoving(c)}>
                      Remove
                    </Button>
                  </td>
                </tr>
              );
            })}
            {creds.length === 0 && (
              <EmptyRow
                colSpan={7}
                icon="Credentials"
                title="No credentials yet"
                description="Tasks will fail until you add an API key or connect a ChatGPT subscription."
              />
            )}
          </tbody>
        </table>
        </div>
      </div>

      <ConfirmDialog
        open={!!removing}
        title="Remove credential"
        body="Future tasks needing this provider will fail until a new key is set."
        confirmLabel="Remove"
        onConfirm={() => removing && onRemove(removing)}
        onCancel={() => setRemoving(null)}
      />
    </div>
  );
}
