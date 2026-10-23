import type { Metadata } from "next";
import { I18nProvider } from "./i18n";
import "./design-tokens.css";
import "./globals.css";
import "./v3.css";
import "./cult.css";
import "./brand.css";

export const metadata: Metadata = {
  title: "Veynod Console",
  description:
    "Human-in-the-loop shared blackboard for multi-agent teams: tasks, state grid, documents, agents and audit.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap"
        />
      </head>
      <body>
        <I18nProvider>{children}</I18nProvider>
      </body>
    </html>
  );
}
