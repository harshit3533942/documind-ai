import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

// Renders LLM answers written in Markdown. react-markdown never renders raw HTML from the text
// (no rehype-raw), so an answer cannot inject <script> or other markup - safe for untrusted text.
const components: Components = {
  p: ({ children }) => <p className="my-2 first:mt-0 last:mb-0">{children}</p>,
  strong: ({ children }) => <strong className="font-semibold text-ink">{children}</strong>,
  ul: ({ children }) => <ul className="my-2 list-disc space-y-1 pl-5">{children}</ul>,
  ol: ({ children }) => <ol className="my-2 list-decimal space-y-1 pl-5">{children}</ol>,
  h1: ({ children }) => <h3 className="mb-1 mt-3 text-base font-semibold first:mt-0">{children}</h3>,
  h2: ({ children }) => <h3 className="mb-1 mt-3 text-base font-semibold first:mt-0">{children}</h3>,
  h3: ({ children }) => <h4 className="mb-1 mt-3 font-semibold first:mt-0">{children}</h4>,
  a: ({ children, href }) => (
    <a href={href} target="_blank" rel="noopener noreferrer" className="text-brand-700 underline">{children}</a>
  ),
  pre: ({ children }) => (
    <pre className="my-2 overflow-x-auto rounded-lg bg-slate-900 p-3 text-[13px] leading-snug text-slate-100 [&_code]:bg-transparent [&_code]:p-0 [&_code]:text-inherit">
      {children}
    </pre>
  ),
  code: ({ children }) => <code className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[13px] text-slate-800">{children}</code>,
  table: ({ children }) => (
    <div className="my-2 overflow-x-auto"><table className="min-w-full border-collapse text-left">{children}</table></div>
  ),
  th: ({ children }) => <th className="border border-slate-200 bg-slate-50 px-2 py-1 font-semibold">{children}</th>,
  td: ({ children }) => <td className="border border-slate-200 px-2 py-1">{children}</td>,
  blockquote: ({ children }) => <blockquote className="my-2 border-l-4 border-slate-200 pl-3 text-ink-muted">{children}</blockquote>,
};

export function Markdown({ text }: { text: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
      {text}
    </ReactMarkdown>
  );
}
