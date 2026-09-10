import { describe, it, expect, vi } from "vitest";
import { newSessionId } from "./sessions";

describe("newSessionId", () => {
  it("returns a non-empty unique string on each call", () => {
    const a = newSessionId();
    const b = newSessionId();
    expect(a).toBeTruthy();
    expect(b).toBeTruthy();
    expect(a).not.toBe(b);
  });
});

it("generates UUID sessions on HTTP where randomUUID is unavailable", () => {
  const getRandomValues = crypto.getRandomValues.bind(crypto);
  vi.stubGlobal("crypto", { getRandomValues });
  try {
    const first = newSessionId();
    expect(first).toMatch(/^sess_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(newSessionId()).not.toBe(first);
  } finally { vi.unstubAllGlobals(); }
});
