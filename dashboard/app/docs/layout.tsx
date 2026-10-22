import "./docs.css";

import type { ReactNode } from "react";

export default function DocsLayout({ children }: { children: ReactNode }) {
  return (
    <>
      {/* First paint before the docs stylesheet arrives: keep the frame dark. */}
      <style>{`html,body{background:#080a0e;color:#eef2f8;color-scheme:dark}`}</style>
      {children}
    </>
  );
}
