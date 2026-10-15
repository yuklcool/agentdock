import { useState } from "react";
import { describe, it, expect } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NANOBOT_FILES, NanobotInitialFiles, nanobotFilesError } from "./NanobotInitialFiles";
import type { NanobotFiles } from "./NanobotInitialFiles";

function Editor() {
  const [files, setFiles] = useState<NanobotFiles>({});
  return <><NanobotInitialFiles value={files} onChange={setFiles} /><output data-testid="stored">{JSON.stringify(files)}</output></>;
}

describe("Nanobot initialization overrides", () => {
  it("edits, uploads, replaces and restores every supported file without storing defaults", async () => {
    const user = userEvent.setup();
    render(<Editor />);
    expect(screen.getAllByText("使用 Nanobot 默认")).toHaveLength(6);
    expect(screen.getByTestId("stored")).toHaveTextContent("{}");
    for (const name of NANOBOT_FILES) {
      await user.click(screen.getByRole("button", { name: `编辑 ${name}` }));
      const content = name === "config.json" ? '{"gateway":{}}' : `Custom ${name}`;
      const editor = screen.getByLabelText(`${name} 内容`);
      await user.clear(editor);
      await user.paste(content);
      expect(JSON.parse(screen.getByTestId("stored").textContent ?? "{}")[name]).toBe(content);
      const replacement = name === "config.json" ? '{"channels":{}}' : `Uploaded ${name}`;
      const file = new File([replacement], name, { type: name === "config.json" ? "application/json" : "text/plain" });
      Object.defineProperty(file, "text", { value: async () => replacement });
      await user.upload(screen.getByLabelText(`上传 ${name}`), file);
      await waitFor(() => expect(screen.getByLabelText(`${name} 内容`)).toHaveValue(replacement));
      await user.click(screen.getByRole("button", { name: `恢复 ${name} 为 Nanobot 默认` }));
      expect(JSON.parse(screen.getByTestId("stored").textContent ?? "{}")).not.toHaveProperty(name);
    }
    expect(screen.getAllByText("使用 Nanobot 默认")).toHaveLength(6);
  });

  it("validates JSON and UTF-8 size and formats an explicit JSON override", async () => {
    expect(nanobotFilesError({ "config.json": "[]" })).toContain("JSON 对象");
    expect(nanobotFilesError({ "SOUL.md": "中".repeat(22000) })).toContain("64 KiB");
    const user = userEvent.setup();
    render(<Editor />);
    await user.click(screen.getByRole("button", { name: "编辑 config.json" }));
    await user.clear(screen.getByLabelText("config.json 内容"));
    await user.paste("broken");
    expect(screen.getByRole("alert")).toHaveTextContent("格式无效");
    await user.clear(screen.getByLabelText("config.json 内容"));
    await user.paste('{"gateway":{}}');
    await user.click(screen.getByRole("button", { name: "格式化 JSON" }));
    expect(screen.getByLabelText("config.json 内容")).toHaveValue('{\n  "gateway": {}\n}');
  });
});
