import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "../../test/server";
import { renderWithProviders } from "../../test/render";
import { SkillArchiveImportDialog } from "./SkillArchiveImportDialog";

describe("SkillArchiveImportDialog", () => {
  it("previews an uploaded package and imports the selected skills", async () => {
    let importedSelected: string[] = [];
    server.use(
      http.post("/v1/skills/archive-discover", ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("filename")).toBe("lighting.zip");
        return HttpResponse.json({
          ok: true,
          truncated: false,
          skills: [
            {
              subpath: "lighting-sql",
              name: "lighting-sql",
              description: "SQL analysis",
              valid: true,
              error: null,
              installed: false,
              bundle_size: 1024,
            },
            {
              subpath: "lighting-health",
              name: "lighting-health",
              description: "Health analysis",
              valid: true,
              error: null,
              installed: true,
              bundle_size: 2048,
            },
          ],
        });
      }),
      http.post("/v1/skills/archive-import", ({ request }) => {
        const url = new URL(request.url);
        importedSelected = url.searchParams.getAll("selected");
        return HttpResponse.json({
          ok: true,
          imported: [
            {
              id: "skl_uploaded",
              name: "lighting-sql",
              description: "SQL analysis",
              enabled: true,
              source_type: "archive",
              source_ref: "lighting.zip",
              source_subpath: "lighting-sql",
              created_at: null,
              updated_at: null,
            },
          ],
        });
      }),
      http.get("/v1/skills", () => HttpResponse.json({ skills: [] })),
    );

    const onClose = vi.fn();
    const { container } = renderWithProviders(
      <SkillArchiveImportDialog open onClose={onClose} />,
    );

    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["not parsed by the mocked API"], "lighting.zip", {
      type: "application/zip",
    });
    fireEvent.change(input, { target: { files: [file] } });

    expect(await screen.findByText("lighting-sql")).toBeInTheDocument();
    expect(screen.getByText("lighting-health")).toBeInTheDocument();
    expect(screen.getByText("已安装")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "导入 1 个技能" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "导入 1 个技能" }));

    await waitFor(() => {
      expect(importedSelected).toEqual(["lighting-sql"]);
      expect(onClose).toHaveBeenCalledOnce();
    });
  });

  it("rejects unsupported archive extensions before calling the API", async () => {
    let called = false;
    server.use(
      http.post("/v1/skills/archive-discover", () => {
        called = true;
        return HttpResponse.json({ ok: true, truncated: false, skills: [] });
      }),
    );

    const { container } = renderWithProviders(
      <SkillArchiveImportDialog open onClose={() => undefined} />,
    );
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(["x"], "skill.rar", { type: "application/octet-stream" })] },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(".zip");
    expect(called).toBe(false);
  });
});
