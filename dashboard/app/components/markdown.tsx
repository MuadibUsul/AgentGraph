"use client";

import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";

const schema = {
  ...defaultSchema,
  tagNames: [
    ...(defaultSchema.tagNames ?? []),
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "th",
    "td",
    "caption",
    "colgroup",
    "col",
    "figure",
    "figcaption",
  ],
  attributes: {
    ...(defaultSchema.attributes ?? {}),
    a: [...(defaultSchema.attributes?.a ?? []), "href", "title"],
    img: [...(defaultSchema.attributes?.img ?? []), "src", "alt", "title"],
    table: [...(defaultSchema.attributes?.table ?? []), "headerRow", "header-row"],
    th: [
      ...(defaultSchema.attributes?.th ?? []),
      "colSpan",
      "rowSpan",
      "colspan",
      "rowspan",
      "align",
    ],
    td: [
      ...(defaultSchema.attributes?.td ?? []),
      "colSpan",
      "rowSpan",
      "colspan",
      "rowspan",
      "align",
    ],
  },
};

const components: Components = {
  a: ({ href, children, ...props }) => {
    const external = typeof href === "string" && /^https?:\/\//i.test(href);
    return (
      <a
        {...props}
        href={href}
        target={external ? "_blank" : undefined}
        rel={external ? "noopener noreferrer" : undefined}
        referrerPolicy="no-referrer"
      >
        {children}
      </a>
    );
  },
  img: ({ src, alt }) => {
    const safe = typeof src === "string" && src.startsWith("/");
    if (!safe) {
      return (
        <a href={typeof src === "string" ? src : undefined} target="_blank" rel="noopener noreferrer">
          {alt || "External image blocked"}
        </a>
      );
    }
    return <img src={src} alt={alt || ""} loading="lazy" referrerPolicy="no-referrer" />;
  },
};

export function MarkdownContent({ content }: { content: string }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeRaw, [rehypeSanitize, schema]]}
        components={components}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
