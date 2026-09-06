import { expect, test } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { server } from "../../test/server";
import { renderWithProviders } from "../../test/render";
import Operations from "./Operations";

test("saves validated policy fields and renders audit history", async () => {
  const policy = { max_private_containers_per_user: 10, daily_token_budget: 0, daily_task_limit: 0, user_daily_token_budget: 0, user_daily_task_limit: 0 };
  let saved: unknown;
  server.use(
    http.get("/v1/operations/policy", () => HttpResponse.json(policy)),
    http.get("/v1/operations/audit", () => HttpResponse.json({ events: [{ id: 1, ts: "2026-09-06T00:00:00Z", action: "personal_agent.created", actor_id: "alice", target_id: "instance" }] })),
    http.put("/v1/operations/policy", async ({ request }) => { saved = await request.json(); return HttpResponse.json(saved); }),
  );
  renderWithProviders(<Operations />);
  const field = await screen.findByLabelText("每人每日任务数");
  await userEvent.clear(field);
  await userEvent.type(field, "20");
  await userEvent.click(screen.getByRole("button", { name: "保存配额" }));
  await waitFor(() => expect(saved).toEqual({ ...policy, user_daily_task_limit: 20 }));
  expect(await screen.findByText("personal_agent.created")).toBeInTheDocument();
});
