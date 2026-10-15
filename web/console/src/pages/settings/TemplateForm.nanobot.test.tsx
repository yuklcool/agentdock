import { describe, it, expect, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { server } from "../../test/server";
import { renderWithProviders } from "../../test/render";
import { AuthProvider } from "../../auth/AuthProvider";
import TemplateForm from "./TemplateForm";

vi.mock("react-router-dom", async (orig) => ({
  ...(await orig<typeof import("react-router-dom")>()),
  useParams: () => ({ id: "tpl_init" }), useNavigate: () => vi.fn(),
}));

describe("Nanobot template file saving", () => {
  it("persists only overrides and leaves runtime selection to container creation", async () => {
    const template = {
      id: "tpl_init", name: "Native initialization", driver: "nanobot", tenant_id: "t",
      model: "legacy-model", image_variant: "slim", mem_limit: "8g", cpus: 3,
      system_prompt: "", system_prompt_mode: "augment", tools: [], skills: [], mcp_servers: [],
      context: {}, limits: {}, is_builtin: false, env_vars: [],
      nanobot_initial_files: { "SOUL.md": "v1" },
      capabilities: {}, driver_template: { available_tools: [], tools_user_editable: false },
      available_tool_specs: [],
    };
    server.use(
      http.get("/v1/auth/me", () => HttpResponse.json({ id: "u", tenant_id: "t", role: "admin", is_staff: false,
        tenant: { id: "t", limits: { allowed_drivers: ["nanobot"] } } })),
      http.get("/v1/templates", () => HttpResponse.json({ templates: [template] })),
      http.get("/v1/templates/tpl_init", () => HttpResponse.json(template)),
    );
    let posted: Record<string, unknown> | null = null;
    server.use(http.patch("/v1/templates/tpl_init", async ({ request }) => {
      posted = await request.json() as Record<string, unknown>;
      return HttpResponse.json(template);
    }));
    renderWithProviders(<AuthProvider><TemplateForm /></AuthProvider>);
    await screen.findByText("Nanobot 初始化文件");
    expect(screen.queryByRole("radiogroup", { name: "Model" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Image variant")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "恢复 SOUL.md 为 Nanobot 默认" }));
    await userEvent.click(screen.getByRole("button", { name: "编辑 USER.md" }));
    await userEvent.type(screen.getByLabelText("USER.md 内容"), "Custom user");
    await userEvent.click(screen.getByRole("button", { name: /Save template/i }));
    await waitFor(() => expect(posted).toMatchObject({
      nanobot_initial_files: { "USER.md": "Custom user" }, model: null,
      image_variant: null, mem_limit: null, cpus: null, env_vars: [],
    }));
  });
});
