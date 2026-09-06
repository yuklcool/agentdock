import { transferableAbortController } from "node:util";
import "@testing-library/jest-dom/vitest";
import { afterAll, afterEach, beforeAll } from "vitest";
import { server } from "./server";

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = globalThis.ResizeObserver ?? ResizeObserverStub;

// jsdom does not implement scrollIntoView.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function () {};
}

// React Router uses Node fetch; keep abort objects in that same realm.
const nativeAbort = transferableAbortController();
globalThis.AbortController = nativeAbort.constructor as typeof AbortController;
globalThis.AbortSignal = nativeAbort.signal.constructor as typeof AbortSignal;
