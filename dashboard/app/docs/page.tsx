import type { Metadata } from "next";

import { DocsView } from "./docs-view";

export const metadata: Metadata = {
  title: "Veynod Docs",
  description: "Guides, MCP setup and API reference for Veynod.",
};

export default function DocsHome() {
  return <DocsView slug="introduction" />;
}
