import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Components } from "react-markdown";
import "./Markdown.css";

const plugins = [remarkGfm];
const components: Components = {
  table: ({ children }) => <div className="markdown-table-wrap"><table>{children}</table></div>,
};

/** Shared by live transcripts, history and task results. ReactMarkdown parses
 * incomplete streamed text on each update; no generated HTML is injected.
 * Keep its default URL filtering and disable raw HTML from model output. */
export const Markdown = memo(function Markdown({ text, className = "" }: { text: string; className?: string }) {
  return <div className={`markdown-body ${className}`}>
    <ReactMarkdown remarkPlugins={plugins} components={components} skipHtml>{text}</ReactMarkdown>
  </div>;
});
