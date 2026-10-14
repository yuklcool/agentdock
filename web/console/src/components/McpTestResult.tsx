import type { McpConnectionTestResult } from "../api/types";
import { Note } from "../ui";

export function McpTestResult({ result }: { result: McpConnectionTestResult }) {
  return (
    <div aria-live="polite" style={{ display: "grid", gap: 8 }}>
      <Note tone={result.status === "success" ? "mint" : "amber"}>
        <strong>{result.status === "success" ? "连接成功" : result.status === "partial" ? "连接成功，工具发现未完成" : "连接测试失败"}</strong>
        <div style={{ marginTop: 4 }}>
          控制端经出口代理测试 · 耗时 {result.duration_ms} ms · {new Date(result.tested_at).toLocaleTimeString()}
        </div>
        {result.server_info && <div>服务：{result.server_info.name} · {result.server_info.version} · 协议：{result.protocol_version}</div>}
        {result.error && <div>{result.error.message}（{result.error.code} / {result.stage}）</div>}
        {result.warnings.map((warning, i) => <div key={i}>{warning}</div>)}
        <div>该结果验证握手与工具发现；实际工具执行和各智能体驱动加载需在运行环境验证。</div>
      </Note>
      {result.status !== "failed" && (
        <details>
          <summary style={{ cursor: "pointer", fontSize: 13 }}>已发现 {result.tool_count}{result.tools_truncated ? "+" : ""} 个工具</summary>
          <div style={{ maxHeight: 240, overflow: "auto", marginTop: 8, display: "grid", gap: 8 }}>
            {result.tools.map((tool, i) => <div key={`${tool.name}-${i}`} style={{ fontSize: 12.5 }}>
              <code>{tool.name}</code>
              {tool.description && <div style={{ color: "var(--muted)", whiteSpace: "pre-wrap" }}>{tool.description}</div>}
            </div>)}
          </div>
        </details>
      )}
    </div>
  );
}
