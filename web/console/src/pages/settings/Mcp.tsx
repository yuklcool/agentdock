import { Fragment, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMcpServers, useDeleteMcpServer, testMcpServer } from "../../api/queries";
import { McpTestResult } from "../../components/McpTestResult";
import { useToast } from "../../components/Toast";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { ApiError } from "../../api/client";
import { Button } from "../../ui/Button";
import { Pill } from "../../ui/Pill";
import { Icons } from "../../ui/Icon";
import type { McpServer, McpConnectionTestResult } from "../../api/types";

function urlHost(url: string): string {
  try { return new URL(url).host; }
  catch { return url; }
}

export default function Mcp() {
  const { data, isLoading } = useMcpServers();
  const del = useDeleteMcpServer();
  const toast = useToast();

  const [deleting, setDeleting] = useState<McpServer | null>(null);
  const [query, setQuery] = useState("");
  const [testing, setTesting] = useState<Set<string>>(new Set());
  const [results, setResults] = useState<Record<string, { signature: string; result: McpConnectionTestResult }>>({});
  const signature = (s: McpServer) => JSON.stringify([s.url, s.auth_type, s.auth_header_name, s.secret_set, s.updated_at]);

  const servers = useMemo(() => data?.mcp_servers ?? [], [data]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return servers;
    return servers.filter(
      (s) =>
        s.name.toLowerCase().includes(q) ||
        s.description.toLowerCase().includes(q),
    );
  }, [servers, query]);

  async function onDelete(s: McpServer) {
    try {
      await del.mutateAsync(s.id);
      toast.success(`Deleted ${s.name}`);
      setDeleting(null);
    } catch (err) {
      toast.error(
        "Couldn't delete MCP server",
        err instanceof ApiError ? err.message : undefined,
      );
    }
  }

  async function onTest(s: McpServer) {
    if (testing.has(s.id)) return;
    setTesting((ids) => new Set(ids).add(s.id));
    setResults((previous) => { const next = { ...previous }; delete next[s.id]; return next; });
    try {
      const result = await testMcpServer({ id: s.id });
      setResults((previous) => ({ ...previous, [s.id]: { signature: signature(s), result } }));
    } catch (err) {
      toast.error("连接测试失败", err instanceof ApiError ? err.message : undefined);
    } finally {
      setTesting((ids) => { const next = new Set(ids); next.delete(s.id); return next; });
    }
  }

  const subtitle =
    isLoading || servers.length === 0
      ? "MCP servers provide tools and context to your agents."
      : `${servers.length} server${servers.length === 1 ? "" : "s"}`;

  return (
    <div className="page">
      {/* Header */}
      <div className="page-title" style={{ display: "flex", alignItems: "flex-end", gap: 12 }}>
        <div>
          <div style={{ fontSize: 22, fontWeight: 800, letterSpacing: "-0.02em" }}>MCP Servers</div>
          <div style={{ fontSize: 13, color: "var(--muted)", marginTop: 4 }}>{subtitle}</div>
        </div>
      </div>

      {isLoading ? (
        <McpSkeleton />
      ) : servers.length === 0 ? (
        <EmptyHero />
      ) : (
        <>
          {/* Toolbar */}
          <div style={{ display: "flex", alignItems: "stretch", gap: 12, flexWrap: "wrap" }}>
            <div className="search-pill fluid-w" style={{ width: 320, maxWidth: "100%" }}>
              <Icons.Search />
              <input
                aria-label="Search MCP servers"
                placeholder="Search MCP servers…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>
            <Link
              to="/settings/mcp/new"
              className="btn btn-primary btn-sm"
              style={{ marginLeft: "auto", gap: 6, padding: "6px 12px 6px 10px" }}
            >
              <Icons.Plus w={14} /> New server
            </Link>
          </div>

          {/* List */}
          <div className="card flush">
            <div className="tbl-wrap">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>Server</th>
                    <th>Host</th>
                    <th>Auth</th>
                    <th>Status</th>
                    <th>最近测试（本页）</th>
                    <th aria-label="Actions" />
                  </tr>
                </thead>
                <tbody>
                  {visible.length === 0 && (
                    <tr>
                      <td
                        colSpan={6}
                        style={{ padding: "28px 14px", textAlign: "center", color: "var(--muted)" }}
                      >
                        No servers match your search.
                      </td>
                    </tr>
                  )}
                  {visible.map((s) => (
                    <Fragment key={s.id}><tr>
                      {/* Server: icon + name + description */}
                      <td>
                        <div style={{ display: "flex", alignItems: "center", gap: 12, minWidth: 0 }}>
                          <div
                            aria-hidden
                            style={{
                              width: 34,
                              height: 34,
                              borderRadius: 9,
                              flex: "0 0 34px",
                              display: "grid",
                              placeItems: "center",
                              color: "var(--ink)",
                              background: "var(--p-300)",
                            }}
                          >
                            <Icons.Web w={17} />
                          </div>
                          <div style={{ minWidth: 0 }}>
                            <div style={{ fontWeight: 600, fontSize: 13.5 }}>{s.name}</div>
                            <div
                              title={s.description}
                              style={{
                                fontSize: 12.5,
                                color: "var(--muted)",
                                marginTop: 1,
                                maxWidth: 360,
                                overflow: "hidden",
                                textOverflow: "ellipsis",
                                whiteSpace: "nowrap",
                              }}
                            >
                              {s.description}
                            </div>
                          </div>
                        </div>
                      </td>

                      {/* Host */}
                      <td>
                        <span
                          style={{
                            fontSize: 12.5,
                            color: "var(--muted)",
                            fontFamily: "var(--font-mono)",
                          }}
                        >
                          {urlHost(s.url)}
                        </span>
                      </td>

                      {/* Auth / secret */}
                      <td>
                        <span style={{ fontSize: 12.5, color: "var(--muted)" }}>
                          {s.secret_set ? "secret set" : "no auth"}
                        </span>
                      </td>

                      {/* Status */}
                      <td>
                        {s.enabled ? (
                          <Pill tone="success">
                            <span className="dot" />
                            enabled
                          </Pill>
                        ) : (
                          <Pill tone="dormant">
                            <span className="dot" />
                            disabled
                          </Pill>
                        )}
                      </td>

                      {/* Actions */}
                      <td style={{ fontSize: 12.5 }}>
                        {testing.has(s.id) ? "测试中…" : results[s.id]?.signature === signature(s)
                          ? (results[s.id].result.status === "success" ? "成功" : results[s.id].result.status === "partial" ? "部分成功" : "失败")
                          : "未测试"}
                      </td>
                      <td>
                        <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                          <Button size="sm" variant="secondary" onClick={() => onTest(s)} disabled={testing.has(s.id)}>
                            {testing.has(s.id) ? "测试中…" : "测试连接"}
                          </Button>
                          <Link
                            to={`/settings/mcp/${s.id}/edit`}
                            className="btn btn-secondary btn-sm"
                          >
                            Edit
                          </Link>
                          <Button
                            size="sm"
                            variant="danger"
                            aria-label={`Delete ${s.name}`}
                            onClick={() => setDeleting(s)}
                          >
                            <Icons.Trash w={14} />
                          </Button>
                        </div>
                      </td>
                    </tr>
                      {results[s.id]?.signature === signature(s) && <tr>
                        <td colSpan={6}><McpTestResult result={results[s.id].result} /></td>
                      </tr>}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}

      <ConfirmDialog
        open={!!deleting}
        title="Delete MCP server"
        body={`Delete "${deleting?.name}"? Agents that use it will lose access to its tools.`}
        confirmLabel="Delete"
        destructive
        onConfirm={() => deleting && onDelete(deleting)}
        onCancel={() => setDeleting(null)}
      />
    </div>
  );
}

function McpSkeleton() {
  return (
    <div className="card flush" aria-busy="true" aria-label="Loading MCP servers">
      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>Server</th>
              <th>Host</th>
              <th>Auth</th>
              <th>Status</th>
              <th aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {[0, 1, 2].map((i) => (
              <tr key={i}>
                <td>
                  <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                    <span className="skel" style={{ width: 34, height: 34, borderRadius: 9 }} />
                    <div style={{ display: "grid", gap: 6 }}>
                      <span className="skel" style={{ width: 130, height: 11 }} />
                      <span className="skel" style={{ width: 200, height: 10 }} />
                    </div>
                  </div>
                </td>
                <td>
                  <span className="skel" style={{ width: 120, height: 11 }} />
                </td>
                <td>
                  <span className="skel" style={{ width: 64, height: 11 }} />
                </td>
                <td>
                  <span className="skel" style={{ width: 64, height: 18, borderRadius: 999 }} />
                </td>
                <td />
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function EmptyHero() {
  return (
    <div
      className="card"
      style={{
        display: "grid",
        placeItems: "center",
        textAlign: "center",
        padding: "56px 24px",
        gap: 6,
      }}
    >
      <div
        aria-hidden
        style={{
          width: 48,
          height: 48,
          borderRadius: 14,
          background: "var(--p-300)",
          color: "var(--ink)",
          display: "grid",
          placeItems: "center",
          marginBottom: 6,
        }}
      >
        <Icons.Web w={24} />
      </div>
      <div style={{ fontSize: 16, fontWeight: 700, letterSpacing: "-0.01em" }}>
        No MCP servers yet
      </div>
      <div style={{ fontSize: 13, color: "var(--muted)", maxWidth: 380 }}>
        MCP servers expose tools and context to your agents over the Model Context Protocol.
        Add one to get started.
      </div>
      <div
        style={{
          display: "flex",
          gap: 8,
          marginTop: 12,
          flexWrap: "wrap",
          justifyContent: "center",
        }}
      >
        <Link
          to="/settings/mcp/new"
          className="btn btn-primary btn-sm"
          style={{ gap: 6, padding: "6px 12px 6px 10px" }}
        >
          <Icons.Plus w={14} /> New server
        </Link>
      </div>
    </div>
  );
}
