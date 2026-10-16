#!/usr/bin/env node
/*
 * 共享记忆 · 接入命令的 Node 薄壳（v2 · S-5.3）。
 *
 * ━━━ 为什么是"壳"而不是把逻辑重写一遍 ━━━
 *
 * `npx` 只能直接执行 Node 程序，所以 bin 入口必须是 JS。
 * 但真正的解析/合并逻辑在 `scripts/connect.py` —— **只有一份实现**，
 * 不存在"JS 认、Python 不认"的漂移。这个文件只做一件事：找到 python、转发参数。
 */
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const script = join(here, "connect.py");
const args = process.argv.slice(2);

const candidates =
  process.platform === "win32"
    ? ["python", "py", "python3"]
    : ["python3", "python", "py"];

for (const cmd of candidates) {
  const r = spawnSync(cmd, [script, ...args], { stdio: "inherit" });
  if (r.error && r.error.code === "ENOENT") continue;
  process.exit(r.status === null ? 1 : r.status);
}

console.error(
  "✗ 没找到 python（试过：" +
    candidates.join(", ") +
    "）。\n" +
    "  本命令只是一层壳，真正的实现在 scripts/connect.py —— 直接用 python 跑它也行。"
);
process.exit(127);
