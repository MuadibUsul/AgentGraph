import type { Metadata } from "next";

import { DocsView } from "./docs-view";

export const metadata: Metadata = {
  title: "AgentGraph Docs",
  description: "Guides, MCP setup and API reference for AgentGraph.",
};

export default function DocsHome() {
  return <DocsView slug="introduction" />;
}
