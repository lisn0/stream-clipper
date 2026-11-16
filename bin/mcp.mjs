#!/usr/bin/env node
import { spawn } from "node:child_process";
import { constants } from "node:os";

const child = spawn("uvx", [
  "--from", "stream-clipper[mcp]==0.1.0",
  "stream-clipping-mcp", ...process.argv.slice(2),
], { stdio: "inherit" });

const signals = ["SIGINT", "SIGTERM"];
const forward = (signal) => child.kill(signal);
for (const signal of signals) {
  process.on(signal, forward);
}

child.once("error", (error) => {
  if (error.code === "ENOENT") {
    console.error("uvx is required. Install uv and put uvx on PATH.");
    process.exitCode = 127;
  } else {
    console.error(`Could not start uvx: ${error.message}`);
    process.exitCode = 1;
  }
});

child.once("close", (code, signal) => {
  for (const handled of signals) {
    process.off(handled, forward);
  }
  process.exitCode ??= signal ? 128 + constants.signals[signal] : code ?? 1;
});
