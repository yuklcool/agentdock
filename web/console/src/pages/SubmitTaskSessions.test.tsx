import { beforeEach, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { renderWithProviders } from "../test/render";
import SubmitTask from "./SubmitTask";

vi.mock("../components/ChatTurn", () => ({ ChatTurn: ({ prompt }: { prompt: string }) => <p>{prompt}</p> }));

const path = "/containers/con_1/submit";
let posted: { prompt: string; session_id?: string }[];
function setup(driver = "vanilla") {
  server.use(
    http.get("/v1/templates", () => HttpResponse.json({ templates: [] })),
    http.get("/v1/containers/:cid", ({ params }) => HttpResponse.json({ id: params.cid, config: { driver, model: "model", tools: [], system_prompt: "" } })),
    http.get("/v1/containers/:cid/tasks", () => HttpResponse.json({ tasks: [] })),
    http.get("/v1/containers/:cid/sessions", () => HttpResponse.json({ sessions: [...new Set(posted.map(p => p.session_id).filter(Boolean))].map(session_id => ({ session_id, driver, task_count: 1, busy: false })) })),
    http.post("/v1/containers/:cid/tasks", async ({ request }) => {
      const body = await request.json() as typeof posted[number];
      posted.push(body);
      return HttpResponse.json({ task_id: `tsk_${posted.length}`, status: "running", session_id: body.session_id });
    }),
  );
}
function Navigation() {
  const location = useLocation();
  const navigate = useNavigate();
  return <><output data-testid="url">{location.pathname + location.search}</output>
    <button onClick={() => navigate(-1)}>Back</button>
    <button onClick={() => navigate(1)}>Forward</button>
    <button onClick={() => navigate("/containers/con_2/submit")}>Other container</button></>;
}
function mount(route = path) {
  return renderWithProviders(<><Navigation /><Routes>
    <Route path="/containers/:cid/submit" element={<SubmitTask />} />
  </Routes></>, { route });
}
const url = () => screen.getByTestId("url").textContent!;
const selected = () => new URL(url(), "http://test").searchParams.get("session");
async function send(text: string) {
  await userEvent.type(await screen.findByLabelText("Prompt"), text);
  await userEvent.click(screen.getByRole("button", { name: /^Send$/ }));
}
async function chooseNew() {
  await userEvent.click(screen.getByRole("button", { name: /^(No session|Session )/ }));
  await userEvent.click(screen.getByRole("menuitem", { name: "New session" }));
}
beforeEach(() => { posted = []; localStorage.clear(); localStorage.setItem("agentdock.submitLayout", "chat"); });

it.each(["api", "vanilla", "opencode", "codex", "claude-code", "nanobot"])("%s chat creates a lazy session once and reuses it", async driver => {
  setup(driver); mount(path + "?keep=value");
  expect(await screen.findByRole("button", { name: "No session" })).toBeInTheDocument();
  expect(posted).toHaveLength(0);
  await send("first");
  await waitFor(() => expect(posted).toHaveLength(1));
  const id = posted[0].session_id;
  expect(id).toMatch(/^sess_/);
  expect(selected()).toBe(id);
  expect(url()).toContain("keep=value");
  await waitFor(() => expect(screen.getByLabelText("Prompt")).toHaveValue(""));
  await send("second");
  await waitFor(() => expect(posted).toHaveLength(2));
  expect(posted[1].session_id).toBe(id);
  await waitFor(() => expect(screen.getByRole("button", { name: /^Session / })).not.toHaveTextContent("发送后创建"));
});

it("new session is lazy, clears the thread, and URL back/forward restores selections", async () => {
  setup(); mount(); await send("original message");
  expect(await screen.findByText("original message")).toBeInTheDocument();
  const old = selected();
  await chooseNew();
  const next = selected();
  expect(next).not.toBe(old);
  expect(posted).toHaveLength(1);
  expect(screen.queryByText("original message")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^Session / })).toHaveTextContent("发送后创建");
  await userEvent.click(screen.getByRole("button", { name: "Back" }));
  expect(selected()).toBe(old);
  expect(screen.getByText("original message")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Forward" }));
  expect(selected()).toBe(next);
  await send("new conversation");
  await waitFor(() => expect(posted[1]?.session_id).toBe(next));
});

it("restores a bookmarked session and reloads it without retaining another container's draft", async () => {
  setup(); const view = mount(path + "?session=sess_saved");
  await send("continue history");
  await waitFor(() => expect(posted[0]?.session_id).toBe("sess_saved"));
  const bookmark = url(); view.unmount(); mount(bookmark);
  await send("after reload");
  await waitFor(() => expect(posted[1]?.session_id).toBe("sess_saved"));
  await waitFor(() => expect(screen.getByLabelText("Prompt")).toHaveValue(""));
  await userEvent.type(screen.getByLabelText("Prompt"), "old draft");
  await userEvent.click(screen.getByRole("button", { name: "Other container" }));
  expect(await screen.findByRole("button", { name: "No session" })).toBeInTheDocument();
  expect(screen.getByLabelText("Prompt")).toHaveValue("");
  await send("other container");
  await waitFor(() => expect(posted).toHaveLength(3));
  expect(posted[2].session_id).not.toBe("sess_saved");
});

it("failed first send retains its lazy id and prompt for retry", async () => {
  setup(); let attempts = 0;
  server.use(http.post("/v1/containers/con_1/tasks", async ({ request }) => {
    posted.push(await request.json() as typeof posted[number]);
    return ++attempts === 1 ? HttpResponse.json({ error: { message: "unavailable" } }, { status: 503 })
      : HttpResponse.json({ task_id: "tsk_retry", status: "running" });
  }));
  mount(); await send("retry me");
  await screen.findByText("Couldn't submit task");
  expect(screen.getByLabelText("Prompt")).toHaveValue("retry me");
  const id = selected();
  await userEvent.click(screen.getByRole("button", { name: /^Send$/ }));
  await waitFor(() => expect(posted).toHaveLength(2));
  expect(posted.map(p => p.session_id)).toEqual([id, id]);
});

it("an in-flight response cannot clear another session's draft or show its turn", async () => {
  setup(); let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  server.use(http.post("/v1/containers/con_1/tasks", async ({ request }) => {
    posted.push(await request.json() as typeof posted[number]);
    await gate;
    return HttpResponse.json({ task_id: "tsk_late", status: "running" });
  }));
  mount(); await send("old request");
  await waitFor(() => expect(posted).toHaveLength(1));
  await chooseNew();
  const input = screen.getByLabelText("Prompt");
  await userEvent.clear(input); await userEvent.type(input, "new draft");
  release();
  await waitFor(() => expect(screen.getByRole("button", { name: /^Send$/ })).toBeEnabled());
  expect(input).toHaveValue("new draft");
  expect(screen.queryByText("old request")).not.toBeInTheDocument();
});

it("Form can explicitly submit a lazy New session", async () => {
  setup(); localStorage.setItem("agentdock.submitLayout", "form");
  mount(path + "?keep=value");
  await screen.findByRole("button", { name: "No session" });
  await chooseNew(); const id = selected();
  expect(posted).toHaveLength(0);
  await userEvent.type(screen.getByLabelText("Prompt"), "one-off");
  await userEvent.click(screen.getByRole("button", { name: /^Submit task$/ }));
  await waitFor(() => expect(posted[0]?.session_id).toBe(id));
});

it("No session clears only the session query parameter and preserves an empty chat", async () => {
  setup(); mount(path + "?keep=value&session=sess_saved");
  await userEvent.click(await screen.findByRole("button", { name: /^Session / }));
  await userEvent.click(screen.getByRole("menuitem", { name: /^No session/ }));
  expect(url()).toBe(path + "?keep=value");
  expect(screen.getByText("Start a conversation")).toBeInTheDocument();
  expect(posted).toHaveLength(0);
});


it("switching to Form during a send preserves newly edited shared draft", async () => {
  setup(); let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  server.use(http.post("/v1/containers/con_1/tasks", async ({ request }) => {
    posted.push(await request.json() as typeof posted[number]);
    await gate;
    return HttpResponse.json({ task_id: "tsk_form_late", status: "running" });
  }));
  mount(); await send("old chat");
  await waitFor(() => expect(posted).toHaveLength(1));
  await userEvent.click(screen.getByRole("button", { name: "Form" }));
  const input = screen.getByLabelText("Prompt");
  await userEvent.clear(input); await userEvent.type(input, "form draft");
  release();
  await waitFor(() => expect(screen.getByRole("button", { name: /^Submit task$/ })).toBeEnabled());
  expect(input).toHaveValue("form draft");
});
