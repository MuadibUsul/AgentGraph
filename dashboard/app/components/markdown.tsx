"use client";

import ReactMarkdown from "react-markdown";
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

export function MarkdownContent({ content }: { content: string }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeRaw, [rehypeSanitize, schema]]}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
