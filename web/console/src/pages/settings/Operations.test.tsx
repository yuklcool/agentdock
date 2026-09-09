import { expect, test } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { server } from "../../test/server";
import { renderWithProviders } from "../../test/render";
import { AuthContext } from "../../auth/useAuth";
import type { Me } from "../../api/types";
import Operations, { type AuditEvent } from "./Operations";
const policy = { max_private_containers_per_user: 10, daily_token_budget: 0, daily_task_limit: 0, user_daily_token_budget: 0, user_daily_task_limit: 0 };
const event: AuditEvent = {
  id: 1, ts: "2026-09-09T01:45:23Z", action: "container.create", action_label: "创建容器",
  actor_type: "admin", actor_id: "usr_alice", actor: { id: "usr_alice", name: "张三", account: "zhangsan", email: "alice@example.com" },
  target: { id: "con_one", type: "container", type_label: "容器", name: "张三的 Nanobot" },
  container: { id: "con_one", name: "张三的 Nanobot" }, tenant_id: "ten_one", status: "success", status_label: "成功",
};
function setup(events: AuditEvent[] = [event]) {
  server.use(http.get("/v1/operations/policy", () => HttpResponse.json(policy)),
    http.get("/v1/operations/audit", () => HttpResponse.json({ events })));
}
test("groups quotas, displays zero as unlimited and saves numeric limits", async () => {
  setup(); let saved: unknown;
  server.use(http.put("/v1/operations/policy", async ({ request }) => { saved = await request.json(); return HttpResponse.json(saved); }));
  renderWithProviders(<Operations />);
  const field = await screen.findByLabelText("每人每日任务数");
  for (const name of ["实例限制", "工作空间每日限额", "单用户每日限额"]) expect(screen.getByRole("heading", { name })).toBeInTheDocument();
  expect(screen.getAllByPlaceholderText("不限额")).toHaveLength(4);
  expect(screen.getAllByText("不限额（保存值为 0）")).toHaveLength(4);
  expect(screen.getByRole("button", { name: "保存更改" })).toBeDisabled();
  await userEvent.type(field, "20");
  await userEvent.click(screen.getByRole("button", { name: "保存更改" }));
  await waitFor(() => expect(saved).toEqual({ ...policy, user_daily_task_limit: 20 }));
  expect(await screen.findByText("配额已保存")).toBeInTheDocument();
});
test("names and account are primary, IDs appear only in keyboard-accessible details", async () => {
  setup(); renderWithProviders(<Operations />);
  expect(await screen.findByText("zhangsan")).toBeInTheDocument();
  expect(screen.getByText("张三的 Nanobot")).toBeInTheDocument();
  expect(screen.queryByText("container.create")).not.toBeInTheDocument();
  expect(screen.queryByText("usr_alice")).not.toBeInTheDocument();
  const trigger = screen.getByRole("button", { name: "查看创建容器详情" });
  await userEvent.click(trigger);
  const dialog = screen.getByRole("dialog", { name: "操作详情" });
  for (const text of ["container.create", "usr_alice", "ten_one"]) expect(within(dialog).getByText(text)).toBeInTheDocument();
  expect(dialog).toHaveFocus();
  await userEvent.keyboard("{Tab}");
  expect(within(dialog).getByRole("button", { name: "关闭" })).toHaveFocus();
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});
test("filters recent events by action, user and resource and clears filters", async () => {
  setup([event, { ...event, id: 2, action: "task.submitted", action_label: "提交任务", actor_id: "usr_bob",
    actor: { id: "usr_bob", name: "李四", account: null, email: "bob@example.com" },
    target: { id: "tsk_two", type: "task", type_label: "任务", name: "照明分析智能体的任务" }, status_label: "已提交" }]);
  renderWithProviders(<Operations />); await screen.findByText("张三的 Nanobot");
  await userEvent.click(screen.getByRole("button", { name: "筛选操作" }));
  await userEvent.click(screen.getByRole("option", { name: "提交任务" }));
  expect(screen.queryByText("张三的 Nanobot", { selector: "strong" })).not.toBeInTheDocument();
  expect(screen.getByText("bob@example.com")).toBeInTheDocument();
  expect(screen.getByText("已提交")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "清除筛选" }));
  await userEvent.click(screen.getByRole("button", { name: "筛选操作者" }));
  await userEvent.click(screen.getByRole("option", { name: "张三 · zhangsan" }));
  expect(screen.queryByText("照明分析智能体的任务")).not.toBeInTheDocument();
  await userEvent.type(screen.getByLabelText("搜索资源"), "不匹配");
  expect(screen.getByText("没有匹配的操作")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "清除筛选" }));
  await userEvent.type(screen.getByLabelText("搜索资源"), "照明");
  expect(screen.getByText("照明分析智能体的任务")).toBeInTheDocument();
});
test("clearing daily limit saves zero; instance count stays required", async () => {
  setup(); let saved: unknown;
  server.use(http.get("/v1/operations/policy", () => HttpResponse.json({ ...policy, daily_task_limit: 50 })),
    http.put("/v1/operations/policy", async ({ request }) => { saved = await request.json(); return HttpResponse.json(saved); }));
  renderWithProviders(<Operations />);
  await userEvent.clear(await screen.findByLabelText("工作空间每日任务数"));
  expect(screen.getByLabelText("每人最多私有实例数")).toBeRequired();
  await userEvent.click(screen.getByRole("button", { name: "保存更改" }));
  await waitFor(() => expect(saved).toEqual(policy));
});
test("error and retry states do not imply a successful empty response", async () => {
  setup(); let failed = true;
  server.use(http.get("/v1/operations/audit", () => failed ? new HttpResponse(null, { status: 500 }) : HttpResponse.json({ events: [] })));
  renderWithProviders(<Operations />);
  expect(await screen.findByText("无法加载审计记录")).toBeInTheDocument();
  expect(screen.queryByText("暂无操作记录")).not.toBeInTheDocument();
  failed = false; await userEvent.click(screen.getByRole("button", { name: "重试审计" }));
  expect(await screen.findByText("暂无操作记录")).toBeInTheDocument();
});
test("failed save keeps edits without echoing server details", async () => {
  setup(); server.use(http.put("/v1/operations/policy", () => HttpResponse.json({ error: { message: "SECRET" } }, { status: 500 })));
  renderWithProviders(<Operations />);
  const input = await screen.findByLabelText("每人每日任务数"); await userEvent.type(input, "8");
  await userEvent.click(screen.getByRole("button", { name: "保存更改" }));
  expect(await screen.findByText("保存失败，请检查输入或稍后重试")).toBeInTheDocument();
  expect(input).toHaveValue(8); expect(screen.queryByText("SECRET")).not.toBeInTheDocument();
});
test("workspace switch discards audit details and unsaved policy", async () => {
  setup(); const user = { active_tenant_id: "one" } as Me;
  const view = renderWithProviders(<AuthContext.Provider value={{ user, isLoading: false }}><Operations /></AuthContext.Provider>);
  await userEvent.type(await screen.findByLabelText("每人每日任务数"), "6");
  await userEvent.click(screen.getByRole("button", { name: "查看创建容器详情" }));
  server.use(http.get("/v1/operations/audit", () => HttpResponse.json({ events: [] })));
  view.rerender(<AuthContext.Provider value={{ user: { ...user, active_tenant_id: "two" }, isLoading: false }}><Operations /></AuthContext.Provider>);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(await screen.findByText("暂无操作记录")).toBeInTheDocument();
  expect(screen.getByLabelText("每人每日任务数")).toHaveValue(null);
});
