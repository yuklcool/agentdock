import { test, expect } from "@playwright/test";

for (const width of [1440, 390]) {
  test(`operations layout and detail at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    const tenant = { id: "ten_one", name: "照明工作空间", status: "active", limits: {} };
    const me = { id: "usr_admin", name: "管理员", email: "admin@example.com", role: "admin", is_staff: false,
      tenant_id: tenant.id, active_tenant_id: tenant.id, tenant, tenants: [{ ...tenant, role: "admin" }], must_change_password: false };
    await page.route("**/v1/**", async route => {
      const path = new URL(route.request().url()).pathname;
      let body: unknown = {};
      if (path === "/v1/auth/me") body = me;
      else if (path === "/v1/operations/policy") body = { max_private_containers_per_user: 10, daily_task_limit: 0,
        daily_token_budget: 0, user_daily_token_budget: 0, user_daily_task_limit: 0 };
      else if (path === "/v1/operations/audit") body = { events: [{ id: 1, ts: "2026-09-09T01:45:23Z",
        action: "container.create", action_label: "创建容器", actor_type: "admin", actor_id: "usr_admin",
        actor: { id: "usr_admin", name: "管理员", account: null, email: "admin@example.com" },
        target: { id: "con_one", type: "container", type_label: "容器", name: "张三的照明智能体" },
        container: { id: "con_one", name: "张三的照明智能体" }, tenant_id: tenant.id, status: "success", status_label: "成功" }] };
      else if (path === "/v1/tenants") body = { tenants: [tenant] };
      else if (path === "/v1/containers") body = { containers: [] };
      await route.fulfill({ json: body });
    });
    await page.goto("/settings/operations");
    await expect(page.getByRole("heading", { name: "配额与审计", exact: true })).toBeVisible();
    await expect(page.getByText("张三的照明智能体", { exact: true })).toBeVisible();
    await expect(page.getByPlaceholder("不限额")).toHaveCount(4);
    const columns = await page.locator(".operations-quota-grid").evaluate(el => getComputedStyle(el).gridTemplateColumns.split(" ").length);
    expect(columns).toBe(width > 1000 ? 3 : 1);
    expect(await page.locator(".operations-page").evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
    await page.screenshot({ path: `test-results/operations-${width}.png`, fullPage: true });
    await page.getByRole("button", { name: "查看创建容器详情" }).click();
    const dialog = page.getByRole("dialog", { name: "操作详情" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText("usr_admin", { exact: true })).toBeVisible();
    await page.screenshot({ path: `test-results/operations-detail-${width}.png`, fullPage: true });
    await page.keyboard.press("Escape");
    await expect(dialog).not.toBeVisible();
  });
}
