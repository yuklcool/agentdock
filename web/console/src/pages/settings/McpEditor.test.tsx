import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import McpEditor from "./McpEditor";
import type { McpConnectionTestResult } from "../../api/types";

const mocks = vi.hoisted(() => ({ save: vi.fn(), fetch: vi.fn(), test: vi.fn(), error: vi.fn() }));
vi.mock("../../api/queries", () => ({
  useSaveMcpServer: () => ({ mutateAsync: mocks.save, isPending: false }),
  fetchMcpServer: mocks.fetch,
  testMcpServer: mocks.test,
}));
vi.mock("../../components/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: mocks.error }),
}));

const success: McpConnectionTestResult = {
  status: "success", test_location: "control_plane", network_route: "egress_proxy",
  stage: "complete", tested_at: "2026-10-14T06:00:00Z", duration_ms: 50,
  server_info: { name: "stub", version: "1" }, protocol_version: "2025-06-18",
  tools: [{ name: "echo", description: "Return text" }], tool_count: 1,
  tools_truncated: false, warnings: [], error: null,
};

function openEditor(id?: string) {
  render(<MemoryRouter initialEntries={[id ? `/mcp/${id}` : "/new"]}>
    <Routes><Route path="/new" element={<McpEditor />} /><Route path="/mcp/:id" element={<McpEditor />} /></Routes>
  </MemoryRouter>);
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.test.mockResolvedValue(success);
  mocks.fetch.mockResolvedValue({
    id: "mcp_1", name: "stub", description: "d", url: "http://mcp.test/mcp",
    auth_type: "bearer", auth_header_name: null, enabled: true, secret_set: true,
  });
});

describe("MCP connection testing", () => {
  it.each(["http", "https"])("tests a %s draft without saving or requiring a name", async (scheme) => {
    openEditor();
    fireEvent.change(screen.getByLabelText("URL"), { target: { value: `${scheme}://mcp.test/mcp` } });
    expect(screen.getByRole("button", { name: "Create server" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    expect(await screen.findByText("连接成功")).toBeInTheDocument();
    expect(mocks.test).toHaveBeenCalledWith({ id: undefined, url: `${scheme}://mcp.test/mcp`, auth_type: "none", auth_header_name: null });
    expect(mocks.save).not.toHaveBeenCalled();
    expect(screen.getByText("echo")).toBeInTheDocument();
  });

  it("invalidates old results after URL changes", async () => {
    openEditor();
    fireEvent.change(screen.getByLabelText("URL"), { target: { value: "http://mcp.test/mcp" } });
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    await screen.findByText("连接成功");
    fireEvent.change(screen.getByLabelText("URL"), { target: { value: "https://other.test/mcp" } });
    expect(screen.queryByText("连接成功")).not.toBeInTheDocument();
  });

  it("uses the saved secret by omission in edit mode", async () => {
    openEditor("mcp_1");
    await screen.findByLabelText("Secret");
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    await screen.findByText("连接成功");
    expect(mocks.test.mock.calls[0][0]).not.toHaveProperty("secret");
    expect(mocks.test.mock.calls[0][0].id).toBe("mcp_1");
    expect(mocks.save).not.toHaveBeenCalled();
  });

  it("tests a new secret without changing saved configuration", async () => {
    openEditor("mcp_1");
    fireEvent.change(await screen.findByLabelText("Secret"), { target: { value: "new-token" } });
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    await screen.findByText("连接成功");
    expect(mocks.test.mock.calls[0][0].secret).toBe("new-token");
    expect(mocks.save).not.toHaveBeenCalled();
  });

  it("prevents duplicate tests while the request is pending", async () => {
    let resolve!: (result: McpConnectionTestResult) => void;
    mocks.test.mockReturnValue(new Promise((done) => { resolve = done; }));
    openEditor();
    fireEvent.change(screen.getByLabelText("URL"), { target: { value: "http://mcp.test/mcp" } });
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    const pending = screen.getByRole("button", { name: "测试中…" });
    expect(pending).toBeDisabled();
    fireEvent.click(pending);
    expect(mocks.test).toHaveBeenCalledTimes(1);
    resolve(success);
    await screen.findByText("连接成功");
  });

  it("shows diagnostic failures in the form", async () => {
    mocks.test.mockResolvedValue({ ...success, status: "failed", tools: [], tool_count: 0, error: { code: "authentication_failed", message: "认证失败，请检查密钥。" } });
    openEditor();
    fireEvent.change(screen.getByLabelText("URL"), { target: { value: "http://mcp.test/mcp" } });
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    expect(await screen.findByText("连接测试失败")).toBeInTheDocument();
    expect(screen.getByText(/认证失败，请检查密钥/)).toBeInTheDocument();
  });

  it("disables testing for malformed URLs", async () => {
    openEditor();
    fireEvent.change(screen.getByLabelText("URL"), { target: { value: "http://" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "测试连接" })).toBeDisabled());
    expect(mocks.test).not.toHaveBeenCalled();
  });
});
