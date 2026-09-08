import { describe, it, expect, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { server } from "../../test/server";
import { renderWithProviders } from "../../test/render";
import Credentials from "./Credentials";

describe("Credentials", () => {
  beforeEach(() => {
    server.use(http.get("/v1/credentials/providers", () => HttpResponse.json({ providers: [
      { id: "anthropic", label: "Anthropic" }, { id: "openai", label: "OpenAI" },
    ] })));
  });
  it("shows provider + last-4 only, never a secret", async () => {
    server.use(http.get("/v1/credentials", () => HttpResponse.json({ credentials: [
      { id: "cred_1", provider: "anthropic", last4: "8f3a", created_by: "Davis", created_at: "t" },
    ] })));
    renderWithProviders(<Credentials />);
    expect(await screen.findByText(/8f3a/)).toBeInTheDocument();
    expect(screen.getAllByText("anthropic").length).toBeGreaterThan(0);
  });

  it("sets a credential by POSTing provider + api_key", async () => {
    server.use(http.get("/v1/credentials", () => HttpResponse.json({ credentials: [] })));
    let body: any = null;
    server.use(http.post("/v1/credentials", async ({ request }) => { body = await request.json(); return HttpResponse.json({ id: "cred_9", provider: body.provider, last4: "xxxx", created_by: "Davis", created_at: "t" }); }));
    renderWithProviders(<Credentials />);
    await userEvent.click(await screen.findByRole("button", { name: /Add API key/i }));
    await userEvent.type(await screen.findByLabelText(/API key/i), "sk-ant-secret");
    await userEvent.type(screen.getByLabelText(/Base URL/i), "https://proxy.example/v1");
    await userEvent.click(screen.getByRole("button", { name: /Save credential/i }));
    await waitFor(() => expect(body).toMatchObject({ provider: "anthropic", api_key: "sk-ant-secret", base_url: "https://proxy.example/v1" }));
  });

  it("edits and clears an existing endpoint without resending the key", async () => {
    const credential = { id: "cred_1", provider: "anthropic", auth_method: "api_key", status: "active",
      last4: "1234", base_url: "https://old.example/v1", created_by: "Davis", created_at: "t" };
    server.use(http.get("/v1/credentials", () => HttpResponse.json({ credentials: [credential] })));
    const bodies: unknown[] = [];
    server.use(http.patch("/v1/credentials/cred_1", async ({ request }) => {
      const body = await request.json() as { base_url: string | null };
      bodies.push(body);
      Object.assign(credential, body);
      return HttpResponse.json(credential);
    }));
    renderWithProviders(<Credentials />);
    expect(await screen.findByText("https://old.example/v1")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Edit Base URL" }));
    expect(screen.queryByLabelText(/^API key/i)).not.toBeInTheDocument();
    const input = screen.getByLabelText(/Base URL/i);
    expect(input).toHaveValue("https://old.example/v1");
    await userEvent.clear(input);
    await userEvent.type(input, "https://new.example/v1");
    await userEvent.click(screen.getByRole("button", { name: /Save credential/i }));
    expect(await screen.findByText("https://new.example/v1")).toBeInTheDocument();
    expect(bodies).toEqual([{ base_url: "https://new.example/v1" }]);
    await userEvent.click(screen.getByRole("button", { name: "Edit Base URL" }));
    await userEvent.clear(screen.getByLabelText(/Base URL/i));
    await userEvent.click(screen.getByRole("button", { name: /Save credential/i }));
    expect(await screen.findByText("Provider default")).toBeInTheDocument();
    expect(bodies[1]).toEqual({ base_url: null });
  });

  it("shows auth method + status badges and an account tail for subscriptions", async () => {
    server.use(http.get("/v1/credentials", () => HttpResponse.json({ credentials: [
      { id: "cred_1", provider: "openai", auth_method: "oauth_subscription", status: "active",
        last4: null, account_tail: "5678", expires_at: "2026-06-04T12:00:00Z",
        created_by: "Davis", created_at: "t" },
    ] })));
    renderWithProviders(<Credentials />);
    expect(await screen.findByText(/5678/)).toBeInTheDocument();
    expect(screen.getAllByText(/subscription/i).length).toBeGreaterThan(0);
  });

  it("starts the ChatGPT device flow and shows the user code", async () => {
    server.use(http.get("/v1/credentials", () => HttpResponse.json({ credentials: [] })));
    server.use(http.post("/v1/credentials/oauth/openai/start", () => HttpResponse.json({
      connection_id: "oac_1", user_code: "ABCD-1234",
      verification_uri: "https://auth.openai.com/codex/device",
      verification_uri_complete: "https://auth.openai.com/codex/device?u=ABCD-1234",
      expires_in: 900, interval: 5,
    })));
    server.use(http.get("/v1/credentials/oauth/openai/connections/oac_1", () =>
      HttpResponse.json({ connection_id: "oac_1", status: "connected", error: null, credential_id: "cred_9" })));
    renderWithProviders(<Credentials />);
    await userEvent.click(await screen.findByRole("button", { name: /Connect ChatGPT/i }));
    expect(await screen.findByText("ABCD-1234")).toBeInTheDocument();
  });
});
