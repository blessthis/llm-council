# seats.yaml — Schema & Validation Specification

> **Status:** spec only, v1. Feeds P3 (`seats/loader.py`, `seats/base.py`) and P4 (`seats` CLI family, installer Phase A).
> **Sources:** PLAN.md §8 (seat schema), Decision #8 (seats = LLM families), #14 (BYO gateway), #16 (one env block per seat), Q4 (pure exec-array args), Q9 (SEATS_FILE), docs/seatspec.md §2-3.

`seats.yaml` is **THE single source of truth** for seat topology **and** secrets. Read via `SEATS_FILE` env (default `~/.blessthis-llm-council/seats.yaml`). File mode **`0600`**.

Key invariants:
- A **seat is an LLM family** (fable, moonshot, minimax, glm, …), NOT a runner. Same `bin` can serve many seats with different creds (Decision #8).
- Exactly **one `agent.env` block per seat** — native→gateway fallback = two seats, preferred-first (Decision #16).
- `agent.args` is a **pure exec-array**: one argv token per element (Q4).
- The runner **prepends `agent.bin` as argv[0]** — no `{bin}` placeholder.
- Gateways are **BYO** (Decision #14): base URLs/keys live in `agent.env`, consumed by the CLI binary.

---

## 1. Full YAML schema with annotated example

```yaml
# ~/.blessthis-llm-council/seats.yaml — mode 0600 — DO NOT COMMIT (contains secrets)

telemetry:                       # mapping, REQUIRED
  enabled: false                 # bool, REQUIRED, explicit. Installer prompts with default true (Decision #22).

version: 1                       # OPTIONAL top-level; absent = 1. Loader rejects version > 1 (rule 35, A9).

seats:                           # mapping seat-name → seat def, REQUIRED, min 1.
                                 # Order meaningful: preferred-first.

  fable-native:
    models: [fable, opus]        # list[str], REQUIRED, min 1. Preferred first.
    agent:
      bin: claude                # str, REQUIRED. argv[0]; PATH name or absolute.
      args:                      # list[str], REQUIRED, min 2. Pure exec-array.
        - "-p"
        - "{prompt}"
        - "--output-format"
        - "json"
        - "--dangerously-skip-permissions"
        - "--model"
        - "{model}"
        - "--add-dir"
        - "{workdir}"
      env:                       # map[str,str], REQUIRED ({} allowed). Verbatim.
        ANTHROPIC_API_KEY: sk-ant-…
        ANTHROPIC_BASE_URL: https://api.anthropic.com

  fable-gw:                      # same bin, DIFFERENT creds (BYO gateway)
    models: [claude-opus-4-8]
    agent:
      bin: claude
      args: ["-p", "{prompt}", "--output-format", "json",
             "--dangerously-skip-permissions",
             "--model", "{model}", "--add-dir", "{workdir}"]
      env:
        ANTHROPIC_BASE_URL: https://llm.blessthis.software
        ANTHROPIC_AUTH_TOKEN: cliproxy-…
        ANTHROPIC_API_KEY: cliproxy-…

  glm:                           # pi runner seat — no env needed
    models: [glm-5.2]
    agent:
      bin: pi
      args:
        - "--mode"
        - "json"
        - "--no-extensions"
        - "--no-skills"
        - "--no-prompt-templates"
        - "--no-context-files"
        - "-p"
        - "{prompt}"
        - "--model"
        - "{model}"
        - "--tools"
        - "read,write,edit,grep,find,ls,bash"
      env: {}                    # pi reads ~/.pi/agent/models.json itself

  # NOTE (pi): pi has NO --add-dir flag. The working directory is simply the
  # subprocess cwd — the runner passes {workdir} as cwd to the spawn call.
  # The safety flags above (--no-extensions, --no-skills,
  # --no-prompt-templates, --no-context-files) keep the headless seat free
  # of host-side extensions/skills; --offline is also available if the seat
  # should never touch the network.

  moonshot:                      # codex runner seat (OpenAI-wire gateway)
    models: [kimi-k3, kimi-k2.7]
    agent:
      bin: codex
      args:
        - "exec"
        - "--json"
        - "--skip-git-repo-check"
        - "-s"
        - "read-only"
        - "--color"
        - "never"
        - "-m"
        - "{model}"
        - "-C"
        - "{workdir}"
        - "{prompt}"
      env:
        OPENAI_BASE_URL: https://llm.blessthis.software/v1
        OPENAI_API_KEY: cliproxy-…

  # gem: gemini runner (needs GEMINI_CLI_TRUST_WORKSPACE; API quota/region errors surface as seat errors)
  #  gem:
  #    models: [gemini-2.5-pro]
  #    agent:
  #      bin: gemini
  #      args: ["-p", "{prompt}", "-m", "{model}"]
  #      env:
  #        GEMINI_CLI_TRUST_WORKSPACE: "true"
  #        GOOGLE_API_KEY: __REPLACE_ME__
```

### Runner notes — gemini

Gemini CLI seat: headless `-p` requires `GEMINI_CLI_TRUST_WORKSPACE=true` in `agent.env` (or `--skip-trust`); also note quota/region API errors surface as seat errors.


### Field reference

| Path | Type | Req? | Description |
|---|---|---|---|
| `telemetry` | map | ✅ | Telemetry settings. Unknown sub-keys rejected. |
| `telemetry.enabled` | bool | ✅ explicit | Literal `true`/`false`; missing = error (forces explicit decision; installer prompts with default true — Decision #22). |
| `seats` | map[str→seat] | ✅ | Min 1 entry. Mapping order = preferred-first seat order. |
| `seats.<name>.models` | list[str] | ✅ | Non-empty. Preferred first, then intra-seat fallbacks. |
| `seats.<name>.agent` | map | ✅ | Unknown sub-keys rejected. |
| `seats.<name>.agent.bin` | str | ✅ | argv[0]; PATH name or absolute path. |
| `seats.<name>.agent.args` | list[str] | ✅ | Pure exec-array. Must contain `{prompt}` and `{model}`. |
| `seats.<name>.agent.env` | map[str→str] | ✅ | May be `{}`. All values strings, injected verbatim. |

### Placeholders

| Placeholder | Req? | Notes |
|---|---|---|
| `{prompt}` | required | The council brief. Must be a WHOLE element. |
| `{model}` | required | Health-picked model for this invocation. |
| `{workdir}` | optional | Substituted as the SUBPROCESS CWD for all runners. May also appear in `agent.args` for runners that need it explicitly (e.g. claude's `--add-dir`). pi has no `--add-dir`. |
| `{session_id}` | optional | Runners may append their own resume tokens instead (claude `--resume`, codex `exec resume` subcommand). Absent + unknown runner → council_ask disabled (warning). |

### Semantics

- **Seat order is preferred-first** (PyYAML mapping order preserved). Decision #16's fallback: `fable-native` listed before `fable-gw`.
- **`models[]` fallback is intra-seat only** — same runner, same env.
- **Runner-specific appends allowed** — discrete argv tokens appended at spawn (resume flags etc.), never shell strings.

---

## 2. Validation rules (complete, numbered)

Severity: **E** = error, **W** = warning. Header includes file path.

**Document structure**

1. **[E]** Not valid YAML → `invalid YAML in seats file: {parser_error}`
2. **[E]** Top level not a mapping → `seats file must be a YAML mapping at the top level, got {type}`
3. **[E]** Unknown top-level key → `unknown top-level key '{key}'; allowed: telemetry, seats`
4. **[E]** `seats` missing/not a mapping → `'seats' must be a mapping of seat name to seat definition`
5. **[E]** `seats` empty → `'seats' must define at least one seat`
6. **[E]** `telemetry` missing → `missing required top-level key 'telemetry' (set telemetry.enabled explicitly; the installer prompts for it)`
7. **[E]** `telemetry.enabled` missing or not bool → `telemetry.enabled must be explicit true or false`
8. **[E]** Unknown key under `telemetry` → `unknown key 'telemetry.{key}'`

**Per seat** (`<s>` = seat name)

9. **[E]** Name not matching `^[a-z0-9][a-z0-9._-]{0,63}$` → `seat '{s}': invalid name; use lowercase letters, digits, '.', '_', '-', starting with a letter or digit`
10. **[E]** Duplicate seat name (raw-text pre-pass / dup-key-rejecting constructor) → `duplicate seat name '{s}'`
11. **[E]** Seat value not a mapping → `seat '{s}': definition must be a mapping`
12. **[E]** Unknown key in seat → `seat '{s}': unknown key '{key}'; allowed: models, agent`
13. **[E]** `models` missing/not list/empty → `seat '{s}': 'models' must be a non-empty list of model names`
14. **[E]** models[i] not a non-empty string → `seat '{s}': models[{i}] must be a non-empty string`
15. **[W]** Duplicate model within seat → `seat '{s}': duplicate model '{m}' ignored`
16. **[E]** `agent` missing/not mapping → `seat '{s}': 'agent' must be a mapping with bin, args, env`
17. **[E]** Unknown key under `agent` → `seat '{s}': unknown key 'agent.{key}'; allowed: bin, args, env`
18. **[E]** `bin` missing/not string/empty → `seat '{s}': agent.bin must be a non-empty string (binary name on PATH or absolute path)`
19. **[W]** `bin` not resolvable (not on PATH / absolute path missing or not executable) → `seat '{s}': agent.bin '{bin}' not found on PATH (or not executable); seat will fail at spawn — run 'blessthis-llm-council doctor'`. Warning, not error — file validity must not depend on which machine edited it. At spawn time a missing binary is a hard seat failure.
20. **[E]** `args` missing/not list/<2 elements → `seat '{s}': agent.args must be a list of at least 2 argv tokens (pure exec-array, one token per element)`
21. **[E]** args[i] not a string → `seat '{s}': agent.args[{i}] must be a string (got {type}); numbers/bools must be quoted`
22. **[E]** Compound element: after placeholder removal (`re.sub(r'\{[a-z_]+\}', '', el)`) contains whitespace → `seat '{s}': agent.args[{i}] '{el}' looks like a compound shell fragment; pure exec-array requires ONE argv token per element (split it into separate list items)`
23. **[E]** Shell metacharacters outside placeholders (`| & ; < > \` $(`) → `seat '{s}': agent.args[{i}] '{el}' contains shell metacharacters; args is exec'd directly, never through a shell — remove the operator and use discrete tokens`
24. **[E]** `{prompt}` absent from all elements → `seat '{s}': agent.args must contain a '{prompt}' placeholder (the council brief has nowhere to go)`
25. **[E]** `{model}` absent → `seat '{s}': agent.args must contain a '{model}' placeholder (the health-picked model has nowhere to go)`
26. **[E]** Unknown placeholder `{foo}` → `seat '{s}': unknown placeholder '{foo}' in agent.args[{i}]; allowed: {prompt}, {model}, {workdir}, {session_id}`
27. **[E]** `{prompt}` not the entire element (e.g. `"pre {prompt}"`) → `seat '{s}': '{prompt}' must be a whole argv token on its own`
28. **[E]** `env` missing → `seat '{s}': agent.env is required (use {} if the runner owns its config, e.g. pi)`
29. **[E]** `env` not a mapping → `seat '{s}': agent.env must be a mapping of NAME: value`
30. **[E]** Env key not matching `^[A-Za-z_][A-Za-z0-9_]*$` → `seat '{s}': invalid env var name '{k}'`
31. **[E]** Env value not a string → `seat '{s}': env.{k} must be a string (got {type}); quote the value, e.g. '{k}: "443"'`. No coercion — silent `yes`→`true`→`"True"` corrupts secrets.
32. **[W]** Env value looks like an unfilled template (`__REPLACE_ME__`, `<your-…>`) → `seat '{s}': env.{k} looks like an unfilled template value`
33. **[W]** No `{session_id}` in args AND unknown runner_kind → `seat '{s}': no {session_id} placeholder and runner '{kind}' has no known resume flag; council_ask (cross-examination) disabled for this seat`
34. **[W]** Multiple seats share the same first model → `seats '{s1}' and '{s2}' both prefer model '{m}'; review whether this is intended`
35. **[E]** Top-level `version` > 1 → `seats.yaml version {v} is not supported by this version of blessthis-llm-council; upgrade blessthis-llm-council` (A9; `version` absent = 1)

Design notes: unknown keys rejected at every level (fail loud in a secrets file); `{prompt}`/`{model}` missing = error; `bin` not on PATH = warning (doctor's job); compound detection is whitespace-after-placeholder-strip (deterministic).

---

## 3. Loader behavior

**Module:** `src/llm_council/seats/loader.py`.

### Read timing — DECIDED: re-read per call with mtime+size cache
- On `council_start`, `chat_start`, `list_seats`, `seat_health`, `seats probe`: stat the file; if mtime AND size match cache, return cached registry. Else re-parse, re-validate, atomically swap cache.
- Rationale: long-lived MCP server + users edit seats via `seats` CLI between councils — pickup without restart, one `stat` in the common case, no inotify.
- Registry is an immutable snapshot (frozen dataclasses). A council in flight keeps its snapshot even if the file changes mid-run.
- `seats probe` bypasses the cache (always fresh read).

### Missing file
Not fatal to the server process (must stay up for `list_seats` to report), but seat-consuming tools return:
```
seats file not found: {path}.
Run `blessthis-llm-council install` (interactive wizard) to create it,
or `blessthis-llm-council seats path` to see where it should live.
```

### Invalid file
Fail fast, per load: cache NOT updated (last-good registry kept, warning logged); triggering tool returns the numbered error list:
```
seats file invalid: {path} (keeping last good config from {mtime}; 3 errors):
  1. seat 'glm': agent.args must contain a '{model}' placeholder
  ...
```
Warnings never block.

### Partial seat failures
Per-seat validation: valid seats load, invalid seats skipped with a warning listing them. Only document-level errors (rules 1-8, 10) reject the whole file.

---

## 4. runner_kind

**Derived, never declared:** `runner_kind = basename(agent.bin)` lowercased, version suffix stripped (`claude`/`pi`/`codex`). Stored in `council_hats.seat_backend`; dispatch key for the Runner subclass; returned by `list_seats()`.

### Unknown bin basename → generic exec runner
1. Spawn `bin + args` with placeholders substituted; final stdout to EOF = answer; usage = zeros; `session_id = None`.
2. Warning rule 33 fires: council_ask disabled, `supports_progress()` = False.
3. `doctor` flags yellow: `unknown runner '<bin>' — using generic exec runner (no resume, no usage, no progress)`.

Rationale: pure exec-array is deliberately runner-agnostic — a fourth CLI needs no code change to try. Known runners add smarts, not permission.

### Explicit `runner:` field override?
**NO for v1.** Only solves renamed/wrapped binaries; invites misuse. If the wrapper case shows up, add optional `runner` key in schema v1.1 (backward-compatible).

---

## 5. Secrets handling

- **Permissions:** created `0600` (explicit `os.chmod` — umask not trusted). Loader warns at load if group/other-readable: `seats file {path} has mode {mode:o}; expected 0600 (contains secrets) — run: chmod 600 {path}`. Warning, not error (Windows).
- **Atomic write:** tmp in same dir → fsync → chmod 0600 → `os.replace()`. Rolling `.bak` (pre-edit content, 0600) from the `seats` editor.
- **Gitignore guidance:** wizard prints + README documents: never commit; ship `seats.example.yaml` with `__REPLACE_ME__` values (rule 32 detects verbatim copies).
- **Comment preservation — DECIDED: `ruamel.yaml` round-trip** for the `seats` CLI editor (comments/anchors/order survive; plain `safe_dump` destroys them). Loader stays on PyYAML `safe_load`; only the editor takes ruamel. New dependency — flag in P1 deps.
- **Secrets never in logs/errors:** error messages quote env KEYS, never VALUES. `doctor` masks values as `sk-…****`.

---

## 6. Open questions (from planner)

1. **Per-seat `working_dir` default?** Lean: defer to v1.1 (optional key, backward-compatible).
2. **Per-seat `limits: {timeout, max_turns}`?** Lean: **YES, add now** — 3 fields of schema, knobs already in the Protocol signature, slow gateway-routed seats are exactly the population that hits 1500s timeouts.
3. **Seat enable/disable flag?** Lean: NO for v1 (commenting out + `seats remove` suffice).
4. **Env value templating from OS env (`${MY_KEY}`)?** Lean: NO for v1 — inline literals only; second secret source defeats Decision #8; wrapper-script pattern documented instead.

---

## Risks

- Rules 22/23 false positives on legitimate spaced args — by design per Q4; error message teaches the fix.
- Seat-order semantics depend on mapping order preservation — keep PyYAML `safe_load`.
- ruamel.yaml = new dependency beyond P1's list — flag when P1 dep edit lands.
- Mtime+size cache can miss same-second same-size edits; `seats probe` bypasses.
