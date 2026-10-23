import type { Metadata } from "next";
import { I18nProvider } from "./i18n";
import "./design-tokens.css";
import "./globals.css";
import "./v3.css";
import "./cult.css";

export const metadata: Metadata = {
  title: "AgentGraph Console",
  description:
    "Human-in-the-loop shared blackboard for multi-agent teams: tasks, state grid, documents, agents and audit.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <I18nProvider>{children}</I18nProvider>
      </body>
    </html>
  );
}
