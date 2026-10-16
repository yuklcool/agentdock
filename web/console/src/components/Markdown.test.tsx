import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { Markdown } from "./Markdown";

describe("Markdown", () => {
  it("renders headings, emphasis, lists, quotes, links and inline code", () => {
    const { container } = render(<Markdown text={'# Report\n\n**bold** and *italic* with `inline`\n\n- first\n- second\n\n1. step\n\n> quote\n\n[docs](https://example.com/docs)'} />);
    expect(screen.getByRole("heading", { name: "Report", level: 1 })).toBeInTheDocument();
    expect(container.querySelector("strong")).toHaveTextContent("bold");
    expect(container.querySelector("em")).toHaveTextContent("italic");
    expect(container.querySelector("code")).toHaveTextContent("inline");
    expect(container.querySelectorAll("ul li")).toHaveLength(2);
    expect(container.querySelector("ol li")).toHaveTextContent("step");
    expect(container.querySelector("blockquote")).toHaveTextContent("quote");
    expect(screen.getByRole("link", { name: "docs" })).toHaveAttribute("href", "https://example.com/docs");
  });

  it("renders GFM tables, task lists and strikethrough", () => {
    const { container } = render(<Markdown text={"| Name | Value |\n| --- | --- |\n| A | 42 |\n\n- [x] finished\n- [ ] pending\n\n~~removed~~"} />);
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "42" })).toBeInTheDocument();
    expect(screen.getAllByRole("checkbox")[0]).toBeChecked();
    expect(screen.getAllByRole("checkbox")[1]).not.toBeChecked();
    expect(container.querySelector("del")).toHaveTextContent("removed");
  });

  it("accepts partial streaming Markdown and completes code without altering its contents", () => {
    const { container, rerender } = render(<Markdown text={"## Working\n\n```python\n  print("} />);
    expect(screen.getByRole("heading", { name: "Working" })).toBeInTheDocument();
    expect(container.querySelector("pre code")?.textContent).toBe("  print(\n");
    rerender(<Markdown text={'## Working\n\n```python\n  print("<tag>")\n```\n\n**Done**'} />);
    expect(container.querySelector("pre code")).toHaveClass("language-python");
    expect(container.querySelector("pre code")?.textContent).toBe('  print("<tag>")\n');
    expect(container.querySelector("strong")).toHaveTextContent("Done");
    expect(container.querySelectorAll("pre")).toHaveLength(1);
  });

  it("does not mount raw HTML or unsafe link and image URLs", () => {
    const { container } = render(<Markdown text={'<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n<iframe src="https://example.com"></iframe>\n\n[bad](javascript:alert%281%29)\n\n![image](data:text/html,bad)'} />);
    expect(container.querySelector("script, iframe, [onerror]")).toBeNull();
    expect(container.querySelector("a")).not.toHaveAttribute("href", expect.stringMatching(/^javascript:/i));
    expect(container.querySelector("img")).not.toHaveAttribute("src", expect.stringMatching(/^data:/i));
  });
});
