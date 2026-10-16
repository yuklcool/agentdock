import { test, expect } from "@playwright/test";

for (const width of [1440, 390]) {
  test(`Chat and task results render Markdown without overflowing at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    const tenant = { id: "ten_one", name: "Workspace", status: "active", limits: {} };
    const code = `const message = "${"long_code_".repeat(60)}";`;
    const output = `# Summary\n\n**Ready** with [docs](https://example.com/docs).\n\n- first\n- second\n\n| Name | Value |\n| --- | --- |\n| Agent | ${"wide_cell_".repeat(25)} |\n\n\`\`\`js\n${code}\n\`\`\``;
    const task = { task_id: "tsk_one", status: "completed", session_id: "sess_one", prompt: "Report",
      created_at: "2026-10-16T00:00:00Z", started_at: "2026-10-16T00:00:00Z", ended_at: "2026-10-16T00:00:01Z",
      tokens_in: 0, tokens_out: 0, iterations_used: 1, result: { success: true, output, files: [] },
      config_snapshot: { driver: "nanobot", model: "test", tools: [] }, metadata: {}, error: null };
    await page.route("**/v1/**", async route => {
      const path = new URL(route.request().url()).pathname;
      let body: unknown = {};
      if (path === "/v1/auth/me") body = { id: "usr_admin", name: "Admin", email: "admin@example.com", role: "admin", is_staff: false,
        tenant_id: tenant.id, active_tenant_id: tenant.id, tenant, tenants: [{ ...tenant, role: "admin" }], must_change_password: false };
      else if (path === "/v1/templates") body = { templates: [] };
      else if (path === "/v1/containers/con_one") body = { id: "con_one", name: "Agent", status: "running", config: task.config_snapshot, metadata: {} };
      else if (path === "/v1/containers/con_one/tasks") body = { tasks: [task] };
      else if (path.endsWith("/sessions")) body = { sessions: [{ session_id: "sess_one", driver: "nanobot", task_count: 1, busy: false }] };
      else if (path.endsWith("/events")) body = { events: [{ seq: 1, type: "assistant_message", ts: task.started_at, payload: { content: [{ type: "text", text: "## Analysis\n\n`inline`" }] } }] };
      else if (path === "/v1/containers/con_one/tasks/tsk_one") body = task;
      else if (path === "/v1/containers") body = { containers: [] };
      await route.fulfill({ json: body });
    });
    await page.goto("/containers/con_one/submit?session=sess_one");
    await page.getByRole("button", { name: "Chat", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Analysis", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Summary", exact: true })).toBeVisible();
    await expect(page.getByRole("table")).toHaveCount(1);
    await expect(page.locator(".ev-result-text strong")).toHaveText("Ready");
    await expect(page.locator(".ev-result-text pre code")).toHaveText(code);
    const fits = await page.locator(".ev-result-text").evaluate(el => {
      const pre = el.querySelector("pre")!;
      const table = el.querySelector(".markdown-table-wrap")!;
      return el.scrollWidth <= el.clientWidth + 1
        && pre.scrollWidth > pre.clientWidth && getComputedStyle(pre).overflowX === "auto"
        && getComputedStyle(table).overflowX === "auto";
    });
    expect(fits).toBe(true);
    await page.locator(".chat-thread").evaluate(el => { el.scrollTop = 0; });
    await page.screenshot({ path: `test-results/markdown-chat-${width}.png`, fullPage: true });
    await page.getByRole("link", { name: "Open full view" }).click();
    await expect(page.locator(".result-output").getByRole("heading", { name: "Summary" })).toBeVisible();
    await expect(page.locator(".result-output pre code")).toHaveText(code);
  });
}
