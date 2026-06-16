---
name: procedure-skill-authoring
description: Author Autonomy Procedure Skills with valid SKILL.md structure.
version: 1.0.0
tags: [skills, authoring, procedure-skill, skill-md]
platforms: [macos, linux, windows]
requires_tools: [filesystem.read, filesystem.read_many, filesystem.tree, filesystem.write, filesystem.patch, filesystem.search_files, filesystem.stat_many, filesystem.syntax_check]
---

# Procedure Skill Authoring

Use this procedure when creating or editing an Autonomy Procedure Skill, bundled
skill template, or candidate skill draft.

Workflow:
- Decide whether the skill belongs in `autonomy/bundled_skills/<name>/SKILL.md`
  as a package template or in `<workspace>/.autonomy/skills/<name>/SKILL.md` as
  active workspace storage.
- Use a lowercase hyphenated skill name and keep the directory name identical
  to frontmatter `name`.
- Include YAML frontmatter with `name`, `description`, `version`, `tags`,
  `platforms`, and `requires_tools`.
- Write a body with `Workflow:`, `Tool use rules:`, `Pitfalls:`, and
  `Outcome checks:` sections.
- Keep the skill procedural: it should guide candidate generation, not execute
  tools, grant permission, or judge outcomes.
- If editing an existing skill, inspect it first and prefer `filesystem.patch`
  for focused changes.

Tool use rules:
- Use `filesystem.tree` or `filesystem.search_files` to find existing peer
  skills before creating a new one.
- Use `filesystem.read_many` to compare a few peer SKILL.md files and match
  local conventions.
- Use `filesystem.stat_many` to confirm the destination skill directory and
  source files before writing.
- Use `filesystem.syntax_check` after changing Python files related to bundled
  skill loading or tests.
- This Procedure Skill is guidance only; write and patch actions still require
  normal approval.

Pitfalls:
- Do not copy Hermes frontmatter fields such as author, license, or nested
  metadata into Autonomy bundled skills unless the Autonomy schema is expanded.
- Do not place active runtime skills under `autonomy/bundled_skills`; that
  directory is package template source.
- Do not place bundled templates under `<workspace>/.autonomy/skills`; that
  directory is active workspace storage.
- Do not create a narrow duplicate when an existing skill can be improved.
- Do not write a skill that depends on unavailable tools; `requires_tools`
  controls whether the skill can be loaded.

Outcome checks:
- The new or edited SKILL.md parses through `ProcedureSkillLibrary`.
- Bundled skill directory name equals frontmatter `name`.
- Required tools are minimal and match actual workflow guidance.
- The skill text says it is guidance only and does not bypass governance.
