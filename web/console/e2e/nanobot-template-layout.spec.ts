import { test, expect } from "@playwright/test";

// DOM-only tests cannot detect a flex item being compressed and clipped.
for (const width of [1440, 1100, 390]) {
  test(`Nanobot initialization files remain usable at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    const tenant = { id: "ten_one", name: "Test workspace", status: "active",
      limits: { allowed_drivers: ["vanilla", "opencode", "codex", "claude-code", "api", "nanobot"] } };
    const template = {
      id: "tpl_nanobot", name: "Nanobot template", driver: "nanobot", tenant_id: tenant.id,
      model: null, system_prompt: "", system_prompt_mode: "augment", tools: [],
      context: {}, limits: {}, skills: [], mcp_servers: [], env_vars: [], nanobot_initial_files: {},
      is_builtin: false, capabilities: { supports_skills: true, supports_mcp: true },
      driver_template: { tools_user_editable: false, supports_context: true }, available_tool_specs: [],
    };
    let saved: Record<string, unknown> | undefined;
    await page.route("**/v1/**", async route => {
      const path = new URL(route.request().url()).pathname;
      let body: unknown = {};
      if (path === "/v1/auth/me") body = { id: "usr_admin", name: "Admin", email: "admin@example.com",
        role: "admin", is_staff: false, tenant_id: tenant.id, active_tenant_id: tenant.id,
        tenant, tenants: [{ ...tenant, role: "admin" }], must_change_password: false };
      else if (path === "/v1/templates") body = { templates: [{ ...template, id: "builtin", is_builtin: true }] };
      else if (path === "/v1/templates/tpl_nanobot") {
        if (route.request().method() === "PATCH") saved = route.request().postDataJSON();
        body = template;
      } else if (path === "/v1/skills") body = { skills: [] };
      else if (path === "/v1/mcp-servers") body = { mcp_servers: [] };
      else if (path === "/v1/containers") body = { containers: [] };
      await route.fulfill({ json: body });
    });
    await page.goto("/settings/templates/tpl_nanobot/edit");
    const section = page.locator("section").filter({ hasText: "Nanobot 初始化文件" });
    await expect(section).toBeVisible();
    // Every control must fit inside the card, rather than behind overflow:hidden.
    for (const name of ["config.json", "AGENTS.md", "SOUL.md", "USER.md", "HEARTBEAT.md", "TOOLS.md"]) {
      const edit = page.getByRole("button", { name: `编辑 ${name}`, exact: true });
      await expect(edit).toBeVisible();
      const contained = await edit.evaluate(el => {
        const card = el.closest("section")!.getBoundingClientRect();
        const button = el.getBoundingClientRect();
        return button.top >= card.top && button.bottom <= card.bottom;
      });
      expect(contained, `${name} must not be clipped by the card`).toBe(true);
      await edit.click();
      await page.getByLabel(`${name} 内容`, { exact: true }).fill(name === "config.json" ? "{}" : `Custom ${name}`);
    }
    await page.screenshot({ path: `test-results/nanobot-template-${width}.png`, fullPage: true });
    await page.getByRole("button", { name: "Save template", exact: true }).click();
    await expect.poll(() => saved?.nanobot_initial_files).toEqual({
      "config.json": "{}", "AGENTS.md": "Custom AGENTS.md", "SOUL.md": "Custom SOUL.md",
      "USER.md": "Custom USER.md", "HEARTBEAT.md": "Custom HEARTBEAT.md", "TOOLS.md": "Custom TOOLS.md",
    });
  });
}
