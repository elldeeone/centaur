#!/usr/bin/env bun

import { chmodSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { resolve } from "node:path";
import { spawnSync } from "node:child_process";

const envFile = resolve(
  process.cwd(),
  process.env.AUTH_BOOTSTRAP_ENV_FILE || ".env.local",
);
const home = homedir();
const loginRequested = process.argv.slice(2).includes("--login");

function readJson(path) {
  if (!existsSync(path)) return null;
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new Error(`${path} is not valid JSON: ${error.message}`);
  }
}

function shellQuote(value) {
  return `'${String(value).replaceAll("'", "'\\''")}'`;
}

function upsertEnvValues(path, values) {
  const lines = existsSync(path) ? readFileSync(path, "utf8").split(/\r?\n/) : [];
  const used = new Set();
  const next = lines.map((line) => {
    const match = /^(export\s+)?([A-Za-z_][A-Za-z0-9_]*)=/.exec(line);
    if (!match || !(match[2] in values)) return line;
    used.add(match[2]);
    return `export ${match[2]}=${shellQuote(values[match[2]])}`;
  });

  for (const [key, value] of Object.entries(values)) {
    if (!used.has(key)) next.push(`export ${key}=${shellQuote(value)}`);
  }

  while (next.length > 0 && next[next.length - 1] === "") next.pop();
  writeFileSync(path, `${next.join("\n")}\n`, { mode: 0o600 });
  chmodSync(path, 0o600);
}

function codexPayload() {
  const path = resolve(home, ".codex", "auth.json");
  const auth = readJson(path);
  if (!auth) return null;
  return { path, value: JSON.stringify(auth) };
}

function claudeAccountPayload() {
  const path = resolve(home, ".claude.json");
  const auth = readJson(path);
  if (!auth || typeof auth.oauthAccount !== "object" || auth.oauthAccount === null) {
    return null;
  }
  const payload = { oauthAccount: auth.oauthAccount };
  if (typeof auth.userID === "string" && auth.userID.trim()) {
    payload.userID = auth.userID;
  }
  return { path, value: JSON.stringify(payload) };
}

function claudeCredentialsPayload() {
  const path = resolve(home, ".claude", ".credentials.json");
  const credentials = readJson(path);
  if (!credentials) return null;
  return { path, value: JSON.stringify(credentials) };
}

function claudeCodeOauthTokenPayload() {
  const token = (process.env.CLAUDE_CODE_OAUTH_TOKEN || "").trim();
  return token ? { path: "CLAUDE_CODE_OAUTH_TOKEN", value: token } : null;
}

function extractClaudeCodeOauthToken(output) {
  const assignment = /CLAUDE_CODE_OAUTH_TOKEN\s*=\s*['"]?([^\s'"]+)['"]?/.exec(
    output,
  );
  if (assignment) return assignment[1];

  return (
    output
      .split(/\r?\n/)
      .map((line) => line.trim())
      .reverse()
      .find((line) => line.length > 50 && !/\s/.test(line)) || ""
  );
}

const updates = {};
const imported = [];
const missing = [];
const loginCommands = [];

const codex = codexPayload();
if (codex) {
  updates.CODEX_AUTH_JSON = codex.value;
  imported.push(["Codex", "CODEX_AUTH_JSON", codex.path]);
} else {
  loginCommands.push(["Codex", "codex", ["login", "--device-auth"]]);
  missing.push([
    "Codex",
    [
      "Run `codex login --device-auth` on the host,",
      "or `bun run auth:bootstrap -- --login` to stream the device flow.",
      "For SSH sessions, open the device URL printed by the Codex CLI,",
      "then rerun `bun run auth:bootstrap`.",
    ].join(" "),
  ]);
}

const claudeAccount = claudeAccountPayload();
const claudeCredentials = claudeCredentialsPayload();
const claudeCodeOauthToken = claudeCodeOauthTokenPayload();
if (claudeCodeOauthToken) {
  updates.CLAUDE_CODE_OAUTH_TOKEN = claudeCodeOauthToken.value;
  imported.push([
    "Claude OAuth token",
    "CLAUDE_CODE_OAUTH_TOKEN",
    claudeCodeOauthToken.path,
  ]);
  if (claudeAccount) {
    updates.CLAUDE_AUTH_JSON = claudeAccount.value;
    imported.push(["Claude account", "CLAUDE_AUTH_JSON", claudeAccount.path]);
  }
} else if (claudeCredentials) {
  updates.CLAUDE_CREDENTIALS_JSON = claudeCredentials.value;
  imported.push([
    "Claude credentials",
    "CLAUDE_CREDENTIALS_JSON",
    claudeCredentials.path,
  ]);
  if (claudeAccount) {
    updates.CLAUDE_AUTH_JSON = claudeAccount.value;
    imported.push(["Claude account", "CLAUDE_AUTH_JSON", claudeAccount.path]);
  }
} else if (claudeAccount) {
  loginCommands.push(["Claude", "claude", ["setup-token"]]);
  missing.push([
    "Claude",
    [
      `Found ${claudeAccount.path}, but it only contains account metadata.`,
      "Sandbox auth needs an OAuth token from `claude setup-token`.",
      "Run `claude setup-token` on the host,",
      "export the emitted CLAUDE_CODE_OAUTH_TOKEN,",
      "or `bun run auth:bootstrap -- --login` to capture setup output.",
      "Then rerun `bun run auth:bootstrap`.",
    ].join(" "),
  ]);
} else {
  loginCommands.push(["Claude", "claude", ["setup-token"]]);
  missing.push([
    "Claude",
    [
      "Run `claude setup-token` on the host,",
      "export the emitted CLAUDE_CODE_OAUTH_TOKEN,",
      "or `bun run auth:bootstrap -- --login` to capture setup output.",
      "For SSH sessions, follow the browser URL printed by Claude Code,",
      "then rerun `bun run auth:bootstrap`.",
    ].join(" "),
  ]);
}

if (Object.keys(updates).length > 0) {
  upsertEnvValues(envFile, updates);
  console.log(`Wrote ${envFile}`);
}

for (const [name, key, path] of imported) {
  console.log(`${name}: imported ${path} into ${key}=[redacted]`);
}

for (const [name, instruction] of missing) {
  console.log(`${name}: local auth not found. ${instruction}`);
}

if (loginRequested && loginCommands.length > 0) {
  for (const [name, command, args] of loginCommands) {
    console.log(`${name}: running ${[command, ...args].join(" ")}`);
    const stdio =
      command === "claude" && args[0] === "setup-token"
        ? ["inherit", "pipe", "inherit"]
        : "inherit";
    const result = spawnSync(command, args, { encoding: "utf8", stdio });
    if (result.error) {
      console.error(`${name}: failed to run ${command}: ${result.error.message}`);
      process.exitCode = 1;
    } else if (result.status !== 0) {
      console.error(`${name}: ${command} exited with status ${result.status}`);
      process.exitCode = result.status ?? 1;
    } else if (command === "claude" && args[0] === "setup-token") {
      const output = result.stdout || "";
      process.stdout.write(output);
      const token = extractClaudeCodeOauthToken(output);
      if (token) {
        upsertEnvValues(envFile, { CLAUDE_CODE_OAUTH_TOKEN: token });
        console.log(`Wrote ${envFile}`);
        console.log("Claude OAuth token: imported setup-token output into CLAUDE_CODE_OAUTH_TOKEN=[redacted]");
      } else {
        console.error("Claude: setup-token completed but no CLAUDE_CODE_OAUTH_TOKEN was found in output.");
        process.exitCode = 1;
      }
    }
  }
  console.log("Rerun `bun run auth:bootstrap` after login completes.");
}

if (imported.length > 0) {
  console.log(
    "Enable opt-in use with CODEX_USE_LOCAL_AUTH=true or CLAUDE_USE_LOCAL_AUTH=true in the API deployment env.",
  );
  console.log(
    "For local Kubernetes, source .env.local before running just bootstrap-secrets so the payloads reach centaur-harness-auth.",
  );
}
