# Autonomy-Native AI System

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
SessionShell -> ConversationLoop -> ConversationRouter
ConversationRouter -> ChatResponder
ConversationRouter -> AgentLoop -> ActionGateway -> ToolRegistry
AgentLoop -> TaskResponder
autonomy run -> AgentLoop -> ActionGateway -> ToolRegistry
AgentLoop -> OutcomeEvaluator
AgentLoop -> LearningLoop -> CuratorDaemon
```

The system separates procedure knowledge, executable experience, and
situation-level composition:

```text
ProcedureSkill -> planning knowledge from SKILL.md
ActionRecipe   -> successful template that can form one Action
Situation Graph -> evidence-backed Recipe composition
```

Hermes Agent is retained only as a read-only engineering reference. It is not
a runtime dependency.

## Requirements

- Python 3.13+
- Ollama or an OpenAI API key for live model-generated candidates

## Commands

```bash
python3.13 -m autonomy
python3.13 -m autonomy chat
python3.13 -m autonomy chat --workspace . --max-steps 5
python3.13 -m autonomy model setup
python3.13 -m autonomy model setup ollama
python3.13 -m autonomy model setup openai-api
python3.13 -m autonomy --db /tmp/autonomy.db doctor
python3.13 -m autonomy run "Analyze why this project's tests fail" --workspace .
python3.13 -m autonomy inspect RUN_ID
python3.13 -m autonomy recipes list
python3.13 -m autonomy recipes activate RECIPE_ID
python3.13 -m autonomy recipes disable RECIPE_ID
python3.13 -m autonomy skills list
python3.13 -m autonomy skills view test-diagnosis
python3.13 -m autonomy skills candidates
python3.13 -m autonomy skills view-candidate CANDIDATE_ID
python3.13 -m autonomy skills approve CANDIDATE_ID
python3.13 -m autonomy skills reject CANDIDATE_ID
python3.13 -m autonomy skills disable SKILL_NAME
```

## Interactive Session

`autonomy` starts a terminal session. Natural language input first flows
through a model router that only decides `chat` or `task`. Chat turns then go
to a separate responder that produces the natural reply without creating a
`run_id` or executing tools. Clear task requests flow into `AgentLoop`, where
each run gets a separate `run_id` and journal; a task responder then turns the
run result into a conversational summary with compact metadata. The
conversation session keeps the recent transcript and linked run summaries
available as context for follow-up requests. `autonomy run "goal"` remains
available for one-shot tasks and automation, and always treats the input as a
task.

Session commands:

```text
/help
/exit
/quit
/doctor
/inspect RUN_ID
/workspace PATH
/max-steps N
/skills
/recipes
```

## Model Provider Setup

The system supports `ollama` and `openai-api`. Run `autonomy model setup` to
choose a provider, endpoint, and model. Re-running setup is the only way to
switch the global provider or model.

Validated global configuration is stored under:

```text
~/.autonomy/config.yaml  # active provider, endpoint, model, and timeout
~/.autonomy/.env         # OpenAI API key, mode 0600
```

Live runs do not read legacy model environment variables and do not accept
per-run provider or model overrides. `autonomy doctor` is the diagnostic entry
point for configuration, credentials, endpoint reachability, and model
availability.

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

The MVP exposes four local software-engineering tools:

- `filesystem.read`
- `filesystem.list`
- `search.text`
- `shell.execute`

Read-only actions are low risk. Unknown shell commands require interactive
approval and are rejected in non-interactive mode.

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

The formal skill loader scans one global store:

```text
~/.autonomy/skills/
```

Each planning round filters skills by platform, required tools, and enabled
state. The model chooses at most three summaries, and only those full documents
are loaded for candidate generation.

Initial global skills can be installed under `~/.autonomy/skills/`:

- `repository-orientation`
- `test-diagnosis`
- `implementation-status-audit`
- `read-only-code-review`

Every run finishes with a lightweight `LearningLoop` review. Achieved runs
with at least two successful outcomes may generate a `new_skill` candidate
under `~/.autonomy/skill-candidates/`. Candidate documents are not scanned or
used until a user approves them with `autonomy skills approve`. Rejected and
approved candidates remain as audit artifacts and are hidden from the default
candidate list.

`CuratorDaemon` runs in the background after each run and uses `SkillCurator`
to consolidate clear duplicate or subcase Skills. Auto-merge is allowed only
when required tools and platforms do not expand and the merged target
`SKILL.md` validates. After a successful merge, the source Skill is deleted
from the formal store; agent prompts do not retain source lineage.

## Test Verification

```bash
python3.13 -m pytest
```
