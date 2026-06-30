# Autonomy - Self-Discipline/Self-Harness Agent Focus on Enterprise (In Active Early-stage development)

<img src="./assets/autonomy_icon.png" alt="Autonomy" width="300">

## Requirements

- Python 3.13+
- Ollama or an API key for a supported OpenAI-compatible provider

## Quickstart: macOS and Linux

Clone the repository, create an isolated Python environment, install this
checkout, configure a model provider, then run the TUI:

```bash
git clone https://github.com/willweimike/autonomy.git
cd autonomy
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
autonomy --help
autonomy model setup
autonomy doctor
autonomy tui
```

This project builds an AI system around a skill-aware autonomy loop:

```text
goal -> candidates -> scored candidate selection -> execution boundary validation -> one action
     -> observation -> outcome evaluation -> agent decision -> learning -> explicit termination
```

The previous Kernel and Runtime concepts have been retired. Interactive
sessions now flow through `ConversationLoop` into `AgentLoop`, while one-shot
`autonomy run` calls the same loop directly. Actual tool execution is routed
through `ActionGateway`, so future loops can propose or initiate action while
sharing the same governed execution boundary.

```text
SessionShell -> ConversationLoop -> AgentLoop -> ActionGateway -> ToolRegistry
AgentLoop -> ConversationResponder
autonomy run -> AgentLoop -> ActionGateway -> ToolRegistry
AgentLoop -> OutcomeEvaluator
AgentLoop -> LearningLoop -> CuratorDaemon
```

The system separates procedure knowledge, executable experience, and
per-turn candidates:

```text
Procedure Skill -> planning knowledge from SKILL.md
ActionRecipe    -> successful single-action template learned from tool use
CandidatePath   -> current-turn candidate, not long-term graph memory
```

## Requirements

- Python 3.13+
- Ollama or an API key for a supported OpenAI-compatible provider

## Quickstart: macOS and Linux

Clone the repository, create an isolated Python environment, install this
checkout, configure a model provider, then run the TUI:

```bash
git clone https://github.com/willweimike/autonomy.git
cd autonomy
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
autonomy --help
autonomy model setup
autonomy doctor
autonomy tui
```

Use `autonomy model setup ollama` if you run Ollama locally, or choose one of
the API providers when you have a provider key. Workspace configuration and
secrets are written under `<workspace>/.autonomy/`.

Browser tools are optional. Install Chromium only if you want website
automation:

```bash
python -m playwright install chromium
autonomy doctor
```

Run the test suite from the activated environment:

```bash
python -m pytest
```

## Commands

```bash
python3.13 -m autonomy
python3.13 -m autonomy tui
python3.13 -m autonomy tui --workspace . --max-steps 5
python3.13 -m autonomy model setup
python3.13 -m autonomy model setup ollama
python3.13 -m autonomy model setup openai-api
python3.13 -m autonomy model setup nvidia
python3.13 -m autonomy model setup openrouter
python3.13 -m autonomy model setup deepseek
python3.13 -m autonomy --db /tmp/autonomy.db doctor
python3.13 -m autonomy run "Analyze why this project's tests fail" --workspace .
python3.13 -m autonomy inspect RUN_ID
python3.13 -m autonomy recipes list              # ActionRecipe commands
python3.13 -m autonomy recipes activate RECIPE_ID
python3.13 -m autonomy recipes disable RECIPE_ID
python3.13 -m autonomy skills list
python3.13 -m autonomy skills install-bundled code-editing process-management systematic-debugging test-driven-development technical-spike api-debugging codebase-documentation requesting-code-review plan writing-plans procedure-skill-authoring browser-navigation website-inspection email-himalaya database-retrieval
python3.13 -m autonomy skills install-clawhub SKILL_SLUG
python3.13 -m autonomy skills install-hermes SKILL_NAME_OR_DOC_URL
python3.13 -m autonomy skills view test-diagnosis
python3.13 -m autonomy skills candidates
python3.13 -m autonomy skills view-candidate CANDIDATE_ID
python3.13 -m autonomy skills approve CANDIDATE_ID
python3.13 -m autonomy skills reject CANDIDATE_ID
python3.13 -m autonomy skills disable SKILL_NAME
```

## Running Autonomy inside Docker Sandboxes

Autonomy supports running inside Docker Sandboxes.

Docker Sandboxes is the recommended sandbox deployment for Autonomy when you
want the whole agent process isolated in a microVM. Autonomy does not change
its runtime path inside the sandbox:

```text
Docker Sandboxes microVM
  -> Autonomy process
    -> ConversationLoop
      -> AgentLoop
        -> ActionGateway
          -> ToolRegistry
```

Install and enter a Docker Sandbox, then install and run Autonomy inside that
environment:

```bash
brew trust docker/tap
brew install docker/tap/sbx
sbx login
sbx run shell .
python3.13 -m pip install -e .
autonomy doctor
autonomy tui
```

The same Autonomy surfaces can run inside the sandbox: `autonomy run`,
`autonomy tui`, Discord bot, Telegram bot, MCP tools, and terminal tools.
Provider credentials should be configured from inside the sandbox workspace
with `autonomy model setup`, so secrets stay scoped to that workspace.

Discord, Telegram, model providers, and external MCP servers need the Docker
Sandboxes network policy to allow their endpoints. Chrome extension Native
Messaging host is launched by host Chrome, so it does not automatically run
inside the Docker Sandbox; full Chrome extension isolation needs a host wrapper
later.

v1 does not provide `.autonomy/sandbox.yaml`, v1 does not implement
`backend: sbx`, and Autonomy does not create, manage, or auto-generate Docker
Sandboxes sessions or policies. A future embedded `backend: sbx` needs separate
validation for stdout, stderr, exit code, timeout, and long-running process
lifecycle behavior.

## Chrome Extension UI

Autonomy includes a local Chrome side panel UI for development.

The extension does not execute tools directly. It talks to the local native
messaging host, and the host routes requests into:

```text
ConversationLoop(interface="chrome") -> AgentLoop -> ActionGateway -> ToolRegistry
```

Host commands:

```bash
autonomy chrome-host
autonomy-chrome-host
```

Development setup:

1. Install this checkout in your active Python environment.
2. Load `chrome-extension/` as an unpacked extension in Chrome.
3. Copy `chrome-extension/native-host.example.json` to Chrome's native messaging host directory.
4. Replace `EXTENSION_ID` with the unpacked extension ID.
5. Replace `path` with the absolute path to the `autonomy-chrome-host` executable from your environment.

Native host name:

```text
com.autonomy.app
```

The native host manifest restricts access to the configured extension origin.
The extension never receives provider API keys or `.autonomy/.env` content.
Approval prompts default to deny on timeout or disconnect.
The panel `status` action currently reports host/session count only; it does not expose model/tool status.

## Discord DM Bot

Autonomy can run as an owner-only Discord DM bot for mobile chat access.
Discord is only a UI adapter:

```text
ConversationLoop(interface="discord") -> AgentLoop -> ActionGateway -> ToolRegistry
```

Install the optional Discord dependency:

```bash
python -m pip install -e ".[discord]"
```

Create a Discord application and bot in the Discord Developer Portal, enable
Message Content Intent for the bot, then add the bot token and your Discord
user id to the workspace secrets file:

```text
<workspace>/.autonomy/.env
```

```dotenv
DISCORD_BOT_TOKEN="your-bot-token"
DISCORD_OWNER_ID="123456789012345678"
```

Keep the file mode at `0600`. Start the bot from the workspace:

```bash
autonomy discord-bot --workspace . --max-steps 12
```

Only `DISCORD_OWNER_ID` can use the bot. Normal DM messages become Autonomy
prompts. DM commands:

```text
!status
!inspect RUN_ID
!reset
```

Approval prompts are sent as Discord Allow/Deny buttons. Pending approvals
default to deny on timeout or shutdown. The bot never sends provider API keys
or `.autonomy/.env` content to Discord.

## Telegram DM Bot

Autonomy can also run as an owner-only Telegram private chat bot. Telegram is
only a UI adapter:

```text
ConversationLoop(interface="telegram") -> AgentLoop -> ActionGateway -> ToolRegistry
```

Install the optional Telegram dependency:

```bash
python -m pip install -e ".[telegram]"
```

Create a bot with BotFather, then add the bot token and your Telegram user id
to the workspace secrets file:

```text
<workspace>/.autonomy/.env
```

```dotenv
TELEGRAM_BOT_TOKEN="your-bot-token"
TELEGRAM_OWNER_ID="123456789"
```

Keep the file mode at `0600`. Start the polling bot from the workspace:

```bash
autonomy telegram-bot --workspace . --max-steps 12
```

Only `TELEGRAM_OWNER_ID` can use the bot. v1 ignores group and channel
messages; use a private chat with the bot. Commands:

```text
/status
/inspect RUN_ID
/reset
```

Approval prompts are sent as Telegram inline Allow/Deny buttons. Pending
approvals default to deny on timeout or shutdown. The bot never sends provider
API keys or `.autonomy/.env` content to Telegram.

## Interactive Session

`autonomy` and `autonomy tui` start the terminal UI. Natural language input now
flows directly into `AgentLoop`; there is no pre-agent `chat`/`task` classifier
that can stop a task before governance runs. The model can either choose
governed tools or return a direct answer through `assistant.respond`, which is
still journaled as a low-risk action. Each turn gets a `run_id`, `ActionGateway`
authorization, outcome evaluation, and audit trail. The conversation session
keeps the recent transcript and linked run summaries available as context for
follow-up requests. `autonomy run "goal"` remains available for one-shot tasks
and automation, and uses the same agent loop.

The TUI wraps the same `ConversationLoop`. It renders a responsive startup banner, a session overview
panel, explicit runtime boundary notes, a compact status rule before each prompt
with turn count, run state, transcript-style response panels, route metadata,
run metadata, an Action trail derived from the run journal, a toggleable
compact/full details mode, a `/` command palette, and skill review queues while
keeping the same runtime boundaries: the UI never executes tools directly, and
all actions still go through
`AgentLoop -> ActionGateway -> ToolRegistry`.

Session commands:

```text
/help
/
/?
/exit
/quit
/doctor
/inspect RUN_ID
/details compact
/details full
/workspace PATH
/max-steps N
/skills
/recipes     # ActionRecipe view
/tools
```

## Model Provider Setup

The system supports `ollama` plus OpenAI-compatible providers: `openai-api`,
`nvidia`, `openrouter`, `deepseek`, `xai`, `zai`, `kimi-coding`, and `alibaba`.
Run `autonomy model setup` from the workspace to choose a provider, endpoint,
and model. Re-running setup is the only way to switch that workspace's provider
or model. The interactive setup shows the current configuration and accepts
either numbered selections or provider/model names.

Validated workspace configuration is stored under:

```text
<workspace>/.autonomy/config.yaml  # active provider, endpoint, model, and timeout
<workspace>/.autonomy/.env         # provider API keys, mode 0600
```

Live runs do not read legacy model environment variables, do not read
`~/.autonomy` as fallback storage, and do not accept per-run provider or model
overrides. `autonomy doctor` is the diagnostic entry point for configuration,
credentials, endpoint reachability, and model availability.

### Ollama

```bash
autonomy model setup ollama
autonomy doctor
autonomy run "Read README.md and summarize the implemented system" \
  --workspace . \
  --max-steps 5 \
  --non-interactive
```

Ollama's base URL must include `/v1`. The default is
`http://127.0.0.1:11434/v1`.

### NVIDIA

```bash
autonomy model setup nvidia
autonomy doctor
autonomy run "Read README.md and summarize the implemented system" \
  --workspace . \
  --max-steps 5 \
  --non-interactive
```

The default NVIDIA endpoint is `https://integrate.api.nvidia.com/v1`, the
default model is `moonshotai/kimi-k2.6`, and the API key is stored as
`NVIDIA_API_KEY` in the workspace `.autonomy/.env` file.

### Other OpenAI-Compatible Providers

```bash
autonomy model setup openrouter
autonomy model setup deepseek
autonomy model setup xai
autonomy model setup zai
autonomy model setup kimi-coding
autonomy model setup alibaba
```

Provider API keys are stored in `.autonomy/.env` using the provider's native
environment variable name, such as `OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`,
`XAI_API_KEY`, `GLM_API_KEY`, `KIMI_API_KEY`, or `DASHSCOPE_API_KEY`.

## Project Context

At run start, Autonomy loads the first workspace guidance file it finds from:

```text
AUTONOMY.md
.autonomy.md
AGENTS.md
agents.md
.cursorrules
```

The content is bounded and passed to planning as project context. It can guide
candidate generation, but it does not grant tool permissions, bypass
`ActionGateway`, or affect approval and outcome evaluation.

## Toolsets

Tool availability is controlled by an Autonomy-native toolset catalog and a
workspace configuration file:

```text
<workspace>/.autonomy/tools.yaml
```

The default enabled toolsets are:

```text
assistant
browser
file
terminal
search
skills
memory
```

Inspect or change toolset exposure with:

```bash
autonomy tools status
autonomy tools enable project
autonomy tools enable database
autonomy tools enable mcp
autonomy tools enable delegate
autonomy tools enable browser
autonomy tools disable terminal
```

The catalog includes implemented `project`, `browser`, `memory`, `database`,
`mcp`, and `delegate` toolsets plus planned Hermes-like toolsets such as
`cronjob` and `computer_use`. Planned or unavailable tools are not exposed to
the agent loop.
Enabling a toolset only controls which implemented and available tools are
visible to planning; it does not grant extra permissions or bypass
`ActionGateway`.
Explicit subagent requests expose `delegate.run`; otherwise the delegate toolset
stays hidden from planning even when enabled.

Configure databases for the `database.retrieve` tool in:

```yaml
# <workspace>/.autonomy/database_connections.yaml
connections:
  sample:
    dialect: sqlite
    path: sample.db
    allowed_tables: [orders]
  warehouse:
    dialect: postgres
    allowed_tables: [orders]
    schema:
      tables:
        orders:
          id: integer
          total: numeric
```

The tool uses SQLGlot to validate and transpile read-only SQL across dialects,
and `action: generate` can ask the configured workspace model to draft SQL from
a natural-language request before validation. Use `action: explain` to inspect a
SQLite query plan before running a complex query. SQLite paths are
workspace-bounded and executable; other dialects support configured schema,
validation, transpilation, and generation until a connector is added.

To import external MCP tools, install the optional SDK and enable the `mcp`
toolset:

```bash
python3.13 -m pip install -e ".[mcp]"
autonomy tools enable mcp
```

Then add servers to `<workspace>/.autonomy/mcp_servers.yaml`:

```yaml
servers:
  filesystem:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
    tools:
      include: ["read_file"]
  remote:
    url: "https://example.test/mcp"
```

Discovered tools are exposed as `mcp_<server>_<tool>` through the normal
`ToolRegistry` and `ActionGateway` path, with `MEDIUM` risk and
`external-mcp` side effects. This imports MCP tools only; Autonomy does not
act as an MCP server and does not implement OAuth, resources, prompts, sampling,
hot reload, SSE, or dynamic `tools/list_changed`.

Tool implementation code is grouped under `autonomy/tools/`:

- `autonomy/tools/registry.py`: `ToolSpec`, `ToolRegistry`, and `ApprovalPolicy`
- `autonomy/tools/local.py`: local registry assembly plus file/search/shell tools
- `autonomy/tools/toolsets/`: toolset-specific implementations such as
  `project`, `browser`, and `process`

The first implemented local software-engineering tools are:

- `filesystem.read`
- `filesystem.read_many`
- `filesystem.list`
- `filesystem.tree`
- `filesystem.stat`
- `filesystem.stat_many`
- `filesystem.diff`
- `filesystem.outline`
- `filesystem.imports`
- `filesystem.symbol_search`
- `filesystem.syntax_check`
- `filesystem.write`
- `filesystem.patch`
- `filesystem.trash`
- `filesystem.mkdir`
- `filesystem.move`
- `filesystem.search_files`
- `search.text`
- `shell.execute`
- `process.start`
- `process.poll`
- `process.log`
- `process.wait`
- `process.stop`
- `git.status`
- `git.diff`
- `git.log`
- `git.show`
- `json.parse`
- `yaml.parse`
- `project.detect`
- `python.test_discover`

`filesystem.read` supports line pagination with optional `offset` and `limit`.
Small files still return raw text. Large or explicitly paginated reads return
`LINE|CONTENT` output with a continuation hint, which keeps model context
bounded during repository analysis. `filesystem.list` also supports `offset`
and `limit` so broad or recursive directory listings can be paged.
`filesystem.read_many` reads up to 12 UTF-8 text files in one bounded JSON
observation with shared line-window options and a total character budget. Use
it for small manifest, README, entrypoint, or config batches.
`filesystem.tree` returns a compact bounded ASCII tree and is the preferred
first step for repository orientation before broad recursive listing.
`filesystem.stat` returns bounded JSON metadata for one workspace path, such as
type, size, modified time, and immediate directory counts, without reading file
content. `filesystem.stat_many` returns the same metadata for up to 50 paths in
one bounded observation, which reduces tool turns when checking whether several
candidate files or directories exist before reading them. `filesystem.stat`,
`filesystem.stat_many`, and `filesystem.read` expose a lightweight file
`revision` token that can be passed as `expected_revision` to
`filesystem.write` or `filesystem.patch` to fail fast if a file changed after it
was inspected.
`filesystem.diff` returns bounded read-only git status and diff information for
the workspace or one path. It omits secret-bearing environment files and should
be preferred over shell `git diff` when checking what changed after edits.
`filesystem.outline` returns a compact Python class/function/method outline for
a file or directory, which helps locate relevant code before reading full files.
`filesystem.imports` summarizes Python import statements for a file or
directory, which helps identify module dependencies and likely integration
points.
`filesystem.symbol_search` searches Python class/function/method definitions by
name, match mode, and symbol kind, which is useful for jumping directly to
relevant code.
`filesystem.syntax_check` checks Python syntax without executing code and is a
cheap post-edit diagnostic before running broader tests.
`filesystem.search_files` and `search.text` also support `offset` and `limit`
so broad searches can be paged instead of flooding the model context.
`filesystem.search_files` can additionally return `output_mode=files_only` or
`output_mode=count`, and `context=N` can include nearby lines around content
matches to reduce follow-up file reads.
`filesystem.patch` defaults to exact replacement. When a recent read proves
the intended lines are present but indentation or surrounding whitespace has
drifted, `match_mode=strip_lines` can match the same line sequence after
trimming each line.
Successful `filesystem.write` and `filesystem.patch` actions against Python
files also include lightweight `syntax_ok` diagnostics in their observation
payloads, without executing code.
`filesystem.trash` moves one workspace file or directory to the system Trash
through the `trash` CLI. Use it for deletion instead of shell `rm`, `rmdir`,
or `rm -rf`; it is medium risk and is only exposed when the `trash` CLI is
available.
`filesystem.mkdir` creates workspace directories and `filesystem.move` renames
or moves one workspace file or directory without overwriting an existing
destination. Use them instead of shell `mkdir` or `mv` commands.
When `filesystem.read`, `filesystem.list`, or search tools receive a missing
workspace path, they include similar path suggestions when available.
Secret-bearing environment files such as `.env`, `.env.local`, and `.envrc`
are blocked from file read/list/search/write/patch tools to avoid putting
credentials into model context. Use `.env.example` when configuration shape is
needed.

Use `shell.execute` for short foreground commands. It runs the command string
through the platform shell, so shell operators such as `||`, `&&`, and pipes
use normal shell semantics. Use `process.start` for dev servers, watchers,
long tests, or other commands that need later inspection through
`process.poll`, `process.log`, or `process.wait`.
`process.stop` terminates managed background processes. Starting and stopping
processes are medium-risk actions and still require approval in interactive
use; non-interactive runs reject them by default.
`shell.execute` bounds stdout and stderr by default and accepts optional
`max_chars` for focused command output, which prevents large build or test logs
from flooding the model context. Shell and managed process output also redacts
common API keys, bearer tokens, credential assignments, and private key blocks
before observations are written to the run journal.

The implemented `project` toolset is read-only and opt-in. It adds bounded
project-inspection helpers for git state, recent commits, commit summaries,
JSON/YAML validation, manifest detection, and Python test command discovery.
Use `autonomy tools enable project` before exposing these tools to planning.

The implemented browser tools use headless Chromium through Playwright:

- `browser.navigate`
- `browser.snapshot`
- `browser.click`
- `browser.type`
- `browser.scroll`
- `browser.back`
- `browser.press`
- `browser.screenshot`
- `browser.get_images`
- `browser.console`
- `browser.dialog`

Install the Python package through the project environment, then install the
Chromium runtime:

```bash
python3.13 -m playwright install chromium
autonomy doctor
```

If Chromium is missing, `doctor` and `tools status` report the browser tools as
unavailable and they are not exposed to planning.

`browser.snapshot` returns URL, title, bounded visible text, and an `elements`
inventory of visible actionable controls. Use optional `full` and `max_chars`
when a compact snapshot is not enough. Browser interaction candidates should
use selectors from this inventory instead of guessing selectors from page text.
`browser.screenshot` captures a PNG under workspace `.autonomy/browser-screenshots/`
when visual evidence is needed.
`browser.get_images` returns page image URLs, alt text, dimensions, and
selectors. `browser.console` returns console output and JavaScript page errors,
or evaluates a small diagnostic JavaScript expression in the current page.
`browser.dialog` accepts or dismisses native JavaScript dialogs reported by
`browser.snapshot`. Browser observations redact secret-like page URLs, image
URLs, console output, and diagnostic expression results before journaling.

Read-only local actions are low risk. File write/patch/trash, mkdir, move, and
browser actions are medium risk. Unknown shell commands require interactive
approval and are rejected in non-interactive mode.
File editing tools are workspace-only and text-only; use them instead of shell
heredocs or in-place shell edits.

Model-generated tool use is represented as an `ActionIntent`:

```text
tool
arguments
purpose optional
```

The model does not provide risk, progress, cost, uncertainty, expected effect,
or outcome judgment. `ActionGateway` derives executable `Action` metadata from
the registered `ToolSpec`, validates the execution boundary, and applies
approval before a single tool action can run.

## Outcome Evaluation

Tool execution returns an `Observation`; the agent loop evaluates that observation
into an `Outcome` with execution status, goal status, reason, evidence, and
confidence. Deterministic agent-side evidence is authoritative. Model assistance
is used only to interpret ambiguous successful observations, and it cannot
override a tool failure.

## Procedure Skills

Procedure Skills are governed `SKILL.md` documents that teach the model how to
plan a class of task. They never execute tools, grant permission, bypass
execution governance, or participate in outcome evaluation.

The formal skill loader scans one workspace store:

```text
<workspace>/.autonomy/skills/
```

Each planning round filters skills by platform, required tools, and enabled
state. The model chooses at most three summaries, and only those full documents
are loaded for candidate generation.

Initial workspace skills can be installed under `<workspace>/.autonomy/skills/`:

- `repository-orientation`
- `test-diagnosis`
- `implementation-status-audit`
- `read-only-code-review`
- `code-editing`
- `process-management`
- `systematic-debugging`
- `test-driven-development`
- `technical-spike`
- `api-debugging`
- `codebase-documentation`
- `requesting-code-review`
- `plan`
- `writing-plans`
- `procedure-skill-authoring`
- `email-himalaya`
- `database-retrieval`

Bundled Procedure Skills are Autonomy-native workflow guidance, adapted from
Hermes as an engineering reference without importing Hermes runtime or skill
files. Bundled skill sources live under
`autonomy/bundled_skills/<skill-name>/SKILL.md`; add a new bundled skill by
creating that directory and matching the YAML frontmatter `name` to the
directory name. Code editing, process, software-engineering, and browser
planning skills can be installed from bundled templates:

```bash
autonomy skills install-bundled code-editing process-management systematic-debugging test-driven-development technical-spike api-debugging codebase-documentation requesting-code-review plan writing-plans procedure-skill-authoring browser-navigation website-inspection email-himalaya database-retrieval
```

These skills require the corresponding enabled and available tools before they
are considered by the agent loop.

Public ClawHub skills can also be installed into the same workspace store:

```bash
autonomy skills install-clawhub SKILL_SLUG
```

The installer downloads from `https://clawhub.ai/api/v1/download`, validates
the archive contains one `SKILL.md`, rejects unsafe archive paths, and installs
the skill enabled by default.

Hermes Agent skill docs can be installed from the public Skills Hub too:

```bash
autonomy skills install-hermes apple-notes
autonomy skills install-hermes https://hermes-agent.nousresearch.com/docs/user-guide/skills/bundled/apple/apple-apple-notes
```

The installer uses `https://hermes-agent.nousresearch.com/docs/api/skills.json`
to resolve the skill, pulls the generated source markdown, extracts the
`Reference: full SKILL.md` section, and installs the skill enabled by default.

During candidate generation, the model receives the enabled and available tool
specs from the live `ToolRegistry`, including descriptions, argument contracts,
toolset, risk level, and side effects. The model still only proposes
`ActionIntent`; execution remains gated by `ActionGateway`.

ActionRecipes are learned single-action templates. They can propose one
`ActionIntent` after being activated through the `recipes` CLI, but they do not
form graph paths and do not participate in governance. There is no long-term
graph path layer for recipes; governance remains in
`ActionGateway`, `ToolSpec`, `ApprovalPolicy`, and outcome evaluation.

Every run finishes with a lightweight `LearningLoop` review. Achieved runs
with at least two successful outcomes may generate a `new_skill` candidate
under `<workspace>/.autonomy/skill-candidates/`. Candidate documents are not
scanned or used until a user approves them with `autonomy skills approve`.
Rejected and approved candidates remain as audit artifacts and are hidden from
the default candidate list.

Model-generated Procedure Skills are always candidate-first. The model can
draft a `SKILL.md` from a successful run, but it cannot approve or activate
that draft. Approval is the boundary that moves a candidate into the formal
Procedure Skill Library.

`CuratorDaemon` runs in the background after each run and uses `SkillCurator`
to consolidate clear duplicate or subcase Skills. Auto-merge is allowed only
when required tools and platforms do not expand and the merged target
`SKILL.md` validates. After a successful merge, the source Skill is deleted
from the formal store; agent prompts do not retain source lineage.

## Test Verification

```bash
python3.13 -m pytest
```
