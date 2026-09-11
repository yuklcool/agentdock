import { test, expect } from "@playwright/test";

test("Chat URL survives reload and back/forward; first and later tasks share a session", async ({ page }) => {
  const tenant = { id: "ten_one", name: "Workspace", status: "active", limits: {} };
  const me = { id: "usr_admin", name: "Admin", email: "admin@example.com", role: "admin", is_staff: false,
    tenant_id: tenant.id, active_tenant_id: tenant.id, tenant, tenants: [{ ...tenant, role: "admin" }], must_change_password: false };
  const posted: { prompt: string; session_id: string }[] = [];
  await page.route("**/v1/**", async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    let body: unknown = {};
    if (path === "/v1/auth/me") body = me;
    else if (path === "/v1/tenants") body = { tenants: [tenant] };
    else if (path === "/v1/templates") body = { templates: [] };
    else if (path === "/v1/containers/con_one") body = { id: "con_one", name: "Agent", status: "running", config: { driver: "nanobot", model: "test", tools: [], system_prompt: "" }, metadata: {} };
    else if (path === "/v1/containers/con_one/tasks" && route.request().method() === "POST") {
      posted.push(route.request().postDataJSON());
      body = { task_id: `tsk_${posted.length}`, status: "completed", session_id: posted.at(-1)!.session_id };
    } else if (path === "/v1/containers/con_one/tasks") body = { tasks: posted.map((p, i) => ({ ...p, task_id: `tsk_${i + 1}`, status: "completed", tokens_in: 0, tokens_out: 0, iterations_used: 0 })).filter(p => !url.searchParams.has("session_id") || p.session_id === url.searchParams.get("session_id")).reverse() };
    else if (path.endsWith("/sessions")) body = { sessions: [...new Set(posted.map(p => p.session_id))].map(session_id => ({ session_id, driver: "nanobot", task_count: 1, busy: false })) };
    else if (path.endsWith("/events")) body = { events: [] };
    else if (path.includes("/tasks/tsk_")) body = { task_id: path.split("/").at(-1), status: "completed", prompt: "test", result: { output: "reply", files: [] }, tokens_in: 0, tokens_out: 0, iterations_used: 0 };
    else if (path === "/v1/containers") body = { containers: [] };
    await route.fulfill({ json: body });
  });
  await page.goto("/containers/con_one/submit");
  await page.getByRole("button", { name: "Chat", exact: true }).click();
  await expect(page.getByRole("button", { name: "No session", exact: true })).toBeVisible();
  await page.getByLabel("Prompt", { exact: true }).fill("first turn");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => posted.length).toBe(1);
  const id = posted[0].session_id;
  expect(id).toMatch(/^sess_/);
  await expect(page).toHaveURL(new RegExp(`session=${id}`));
  await page.reload();
  await expect(page.getByText("first turn", { exact: true })).toBeVisible();
  await page.getByLabel("Prompt", { exact: true }).fill("second turn");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => posted.length).toBe(2);
  expect(posted[1].session_id).toBe(id);
  await page.locator(".session-picker-trigger").click();
  await page.getByRole("menuitem", { name: "New session" }).click();
  await expect(page.getByText("first turn", { exact: true })).toHaveCount(0);
  await expect(page.locator(".session-picker-trigger")).toContainText("发送后创建");
  const freshUrl = page.url();
  expect(posted.length).toBe(2);
  await page.goBack();
  await expect(page.getByText("first turn", { exact: true })).toBeVisible();
  await page.goForward();
  await expect(page).toHaveURL(freshUrl);
  await expect(page.getByText("first turn", { exact: true })).toHaveCount(0);
});
