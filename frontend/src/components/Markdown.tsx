import ReactMarkdown, { type Components } from "react-markdown";
import type { ReactNode } from "react";
import remarkGfm from "remark-gfm";

function ExternalLink({ href, children }: { href?: string; children?: ReactNode }) {
  return <a href={href} target="_blank" rel="noopener noreferrer" className="text-brand-700 underline">{children}</a>;
}

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
  a: ({ children, href }) => <ExternalLink href={href}>{children}</ExternalLink>,
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

/** Prefix of the internal links that stand for citation markers ("[2]" -> "[2](#cite-2)"). */
export const CITE_HREF = "#cite-";

export function Markdown({ text, onCite }: { text: string; onCite?: (n: number) => void }) {
  const withCites: Components = onCite
    ? {
        ...components,
        a: (props) =>
          props.href?.startsWith(CITE_HREF) ? (
            <button
              type="button"
              onClick={() => onCite(Number(props.href!.slice(CITE_HREF.length)))}
              className="mx-0.5 inline-flex h-4 min-w-4 -translate-y-1 items-center justify-center rounded border border-brand-300 bg-brand-50 px-1 align-baseline text-[10px] font-semibold text-brand-800 hover:bg-brand-100"
              aria-label={`Show source ${props.href!.slice(CITE_HREF.length)}`}
            >
              {props.children}
            </button>
          ) : (
            <ExternalLink href={props.href}>{props.children}</ExternalLink>
          ),
      }
    : components;
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={withCites}>
      {text}
    </ReactMarkdown>
  );
}
