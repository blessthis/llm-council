/**
 * regen-agents extension — project-local hook for blessthis-llm-council.
 *
 * Watches edit/write tool results on agents/pi/*.md (canonical agent source).
 * When the main agent edits a canonical file, this hook injects a user
 * message asking the main agent to spawn regen subagents for each non-pi host.
 *
 * The hook does NOT spawn subagents itself — it only nudges the main agent,
 * which then spawns subagents in parallel (one per host). Each subagent runs
 * the /regen-for-host <host> prompt-template.
 *
 * Placement: .pi/extensions/regen-agents/index.ts (project-local).
 * Auto-discovered after project trust. /reload to pick up edits.
 */
import path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Hosts to regenerate (every host except pi, which is canonical/source).
const TARGET_HOSTS = ["claude", "codex", "cursor", "copilot", "gemini"];

// Match agents/pi/blessthis-council-*.md (canonical source files only).
function isCanonicalAgentEdit(input: any, cwd: string): boolean {
  const p: string | undefined = input?.path;
  if (!p) return false;
  const abs = path.isAbsolute(p) ? p : path.resolve(cwd, p);
  const expectedDir = path.join(cwd, "agents", "pi");
  const file = path.basename(abs);
  return (
    path.dirname(abs) === expectedDir &&
    file.startsWith("blessthis-council-") &&
    file.endsWith(".md")
  );
}

// Debounce: multiple edits in one turn should produce ONE nudge, not N.
let pendingNudge: { timeout: any; paths: Set<string> } | null = null;

export default function (pi: ExtensionAPI) {
  pi.on("tool_result", async (event, ctx) => {
    // Only react to edit/write tool calls that succeeded.
    if (event.toolName !== "edit" && event.toolName !== "write") return;
    if (event.isError) return;
    if (!isCanonicalAgentEdit(event.input, ctx.cwd)) return;

    const editedPath: string = event.input.path;
    const rel = path.relative(ctx.cwd, path.resolve(ctx.cwd, editedPath));
    const file = path.basename(rel);

    // Coalesce edits within a short window (300ms) so a multi-file edit
    // produces a single nudge listing all changed files.
    if (!pendingNudge) {
      pendingNudge = { timeout: null, paths: new Set() };
    }
    pendingNudge.paths.add(file);

    if (pendingNudge.timeout) clearTimeout(pendingNudge.timeout);
    pendingNudge.timeout = setTimeout(() => {
      const files = [...(pendingNudge?.paths ?? [])].sort();
      pendingNudge = null;
      if (files.length === 0) return;

      const fileList = files.map((f) => `agents/pi/${f}`).join(", ");
      const hostList = TARGET_HOSTS.join(", ");

      // Nudge the main agent. It will spawn parallel subagents, each running
      // /regen-for-host <host>. The main agent is in charge of orchestration.
      const nudge =
        `[regen-agents hook] Canonical agent file(s) edited: ${fileList}.\n` +
        `Regenerate the derived files for every supported host: ${hostList}.\n` +
        `For EACH host, spawn a subagent with the task: "Run /regen-for-host <host>" ` +
        `(replace <host> with the actual host name). Spawn all 5 subagents in parallel.\n` +
        `Do not edit agents/pi/ — that is canonical. Only update agents/<host>/.`;

      pi.sendUserMessage(nudge);
    }, 300);
  });
}
