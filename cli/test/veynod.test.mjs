import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import {
  claudeDesktopConfigPath,
  claudeDesktopEntry,
  cursorEntry,
  makeKeyPair,
  mergeServer,
  openSealed,
  parseArgs,
  sealForTest,
} from "../bin/veynod.mjs";

test("a sealed key opens only with the matching private key", () => {
  const mine = makeKeyPair();
  const sealed = sealForTest("agt_example", mine.publicRaw);
  assert.equal(openSealed(sealed, mine.privateKey, mine.publicRaw), "agt_example");
  const other = makeKeyPair();
  assert.throws(() => openSealed(sealed, other.privateKey, other.publicRaw));
  assert.throws(() => openSealed("v2.a.b.c", mine.privateKey, mine.publicRaw), /format/);
});

test("arguments", () => {
  const options = parseArgs(["init", "--client", "cursor,print", "--host=veynod.example/", "--no-open"]);
  assert.equal(options.command, "init");
  assert.deepEqual(options.clients, ["cursor", "print"]);
  assert.equal(options.host, "https://veynod.example");
  assert.equal(options.open, false);
  assert.throws(() => parseArgs(["init", "--client", "vim"]), /unknown client/);
  assert.throws(() => parseArgs(["init", "--host"]), /needs a value/);
});

test("merging keeps other servers and leaves a backup", () => {
  const dir = mkdtempSync(join(tmpdir(), "veynod-"));
  const path = join(dir, "nested", "mcp.json");
  mergeServer(path, cursorEntry("https://x/mcp/", "agt_1"));
  writeFileSync(path, JSON.stringify({ theme: "dark", mcpServers: { other: { url: "o" } } }));
  mergeServer(path, cursorEntry("https://x/mcp/", "agt_2"));
  const config = JSON.parse(readFileSync(path, "utf8"));
  assert.equal(config.theme, "dark");
  assert.deepEqual(config.mcpServers.other, { url: "o" });
  assert.equal(config.mcpServers.veynod.headers.Authorization, "Bearer agt_2");
  assert.ok(existsSync(`${path}.bak`));
  writeFileSync(path, "{nope");
  assert.throws(() => mergeServer(path, {}), /not valid JSON/);
});

test("client config shapes", () => {
  const desktop = claudeDesktopEntry("https://x/mcp/", "agt_3");
  assert.deepEqual(desktop.args.slice(0, 3), ["-y", "mcp-remote", "https://x/mcp/"]);
  assert.equal(desktop.env.VEYNOD_AUTH, "Bearer agt_3");
  assert.ok(claudeDesktopConfigPath("/home/u", "darwin").includes("Application Support/Claude"));
  assert.ok(claudeDesktopConfigPath("/home/u", "linux").endsWith(".config/Claude/claude_desktop_config.json"));
});
