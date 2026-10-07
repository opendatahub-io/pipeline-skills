# Pipeline Skills

Agentic-ci skills for AIPCC pipeline failure analysis. Consumed as a Claude Code plugin by [pipeline-failure-analyzer](https://github.com/opendatahub-io/pipeline-failure-analyzer).

## Skills

| Skill | Purpose |
|-------|---------|
| `pipeline-grouping` | Group failed pipeline jobs by error similarity |
| `pipeline-rca` | Root cause analysis for a single error group |
| `wheel-failure-triage` | Collect structured wheel failures and hand one audit report to PFA |

## Workspace contract

Skills expect the orchestrator to prepare a workspace at `/workspace/` with:

- `_context/` — Dynamic context files written by the orchestrator
- `jobs/` or `groups/` — Job trace logs and preprocessed errors
- `pipeline-context.json` — Pipeline metadata

See each skill's SKILL.md for the specific context files it reads.

`wheel-failure-triage` also exposes deterministic Python helpers for CI. Its
collector runs in the source checkout with GitLab CI environment variables;
the audit runs from the collector's artifacts. It does not need an LLM.

## Conventions

- Skills are self-contained: prompts, scripts, and references live together under `skills/<name>/`
- SKILL.md uses YAML frontmatter (name, description, allowed-tools) + markdown prompt body
- Scripts should be executable standalone for testing
