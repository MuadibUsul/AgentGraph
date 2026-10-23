import type { Metadata } from "next";

import { DocsView } from "../docs-view";
import { pageTitle, site } from "../site";

export const dynamicParams = false;

export function generateStaticParams() {
  return Object.keys(site.pages)
    .filter((slug) => slug !== "introduction")
    .map((slug) => ({ slug }));
}

export function generateMetadata({ params }: { params: { slug: string } }): Metadata {
  return {
    title: `${pageTitle(params.slug)} · Veynod Docs`,
    description: site.pages[params.slug]?.summary.en,
  };
}

export default function DocsPage({ params }: { params: { slug: string } }) {
  return <DocsView slug={params.slug} />;
}
