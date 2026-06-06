# Autonomy-Native AI System

This project builds an AI system around one governed autonomy loop:

```text
goal -> candidates -> scored candidate selection -> execution boundary validation -> one action
     -> observation -> verification -> learning -> explicit termination
```

`AutonomyRuntime` is the core agent runtime and the only component allowed to
activate actions. The previous Kernel concept has been retired; interactive
sessions now flow through `ConversationLoop` into `AutonomyRuntime`, while
one-shot `autonomy run` calls the same runtime directly.

The system separates procedure knowledge, executable experience, and
situation-level composition:

```text
ProcedureSkill -> planning knowledge from SKILL.md
ActionRecipe   -> verified template that can form one Action
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

`autonomy` starts a terminal session. Natural language input flows through a
session-level conversation loop before creating a governed run. Each run still
gets a separate `run_id` and journal, while the conversation session keeps the
recent transcript and linked run summaries available as context for follow-up
requests. `autonomy run "goal"` remains available for one-shot tasks and
automation.

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
or verification plan. `AutonomyRuntime` derives executable `Action` metadata
from the registered `ToolSpec`, validates the execution boundary, and applies
approval before a single tool action can run.

## Procedure Skills

Procedure Skills are governed `SKILL.md` documents that teach the model how to
plan a class of task. They never execute tools, grant permission, bypass
execution governance, or participate in verification.

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

An achieved run with at least two verified transitions may generate a candidate
under `~/.autonomy/skill-candidates/`. Candidate documents are not scanned or
used until a user approves them with `autonomy skills approve`. Rejected and
approved candidates remain as audit artifacts and are hidden from the default
candidate list.

## Verification

```bash
python3.13 -m pytest
```
