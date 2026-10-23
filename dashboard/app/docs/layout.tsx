import "./docs.css";

import type { ReactNode } from "react";

export default function DocsLayout({ children }: { children: ReactNode }) {
  return (
    <>
      {/* First paint before the docs stylesheet arrives: keep the frame dark. */}
      <style>{`html,body{background:#0a0b0f;color:#e8eaf0;color-scheme:dark}`}</style>
      {children}
    </>
  );
}
