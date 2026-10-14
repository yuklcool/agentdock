import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import Mcp from "./Mcp";

const testConnection = vi.hoisted(() => vi.fn());

vi.mock("../../components/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn() }),
}));

vi.mock("../../api/queries", () => ({
  useMcpServers: () => ({
    data: { mcp_servers: [
      { id: "mcp_1", name: "linear", description: "Linear MCP", url: "https://mcp.linear.app/mcp",
        auth_type: "bearer", auth_header_name: null, secret_set: true, enabled: true,
        created_at: null, updated_at: null },
    ] },
    isLoading: false,
  }),
  testMcpServer: testConnection,
  useDeleteMcpServer: () => ({ mutate: vi.fn(), isPending: false }),
}));

describe("Mcp list", () => {
  it("renders the server name and host", () => {
    render(<MemoryRouter><Mcp /></MemoryRouter>);
    expect(screen.getByText("linear")).toBeInTheDocument();
    expect(screen.getByText(/mcp\.linear\.app/)).toBeInTheDocument();
  });
  it("tests a saved server and keeps enable state separate from the diagnostic", async () => {
    testConnection.mockResolvedValue({
      status: "failed", test_location: "control_plane", network_route: "egress_proxy",
      tested_at: "2026-10-14T06:00:00Z", duration_ms: 5, stage: "initialize",
      server_info: null, protocol_version: null, tools: [], tool_count: 0,
      tools_truncated: false, warnings: [], error: { code: "timeout", message: "连接超时" },
    });
    render(<MemoryRouter><Mcp /></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    expect(await screen.findByText("连接测试失败")).toBeInTheDocument();
    expect(testConnection).toHaveBeenCalledWith({ id: "mcp_1" });
    expect(screen.getByText("enabled")).toBeInTheDocument();
  });
});
