# Output Conventions

Read this file before writing section files or `finding.json`. It covers log quoting, feedback observations, a complete finding example, and how to record `resources_used`.

### Log Analysis Guidelines

- Process all logs through `clean-log.py` before analysis. For additional context, use targeted commands (`grep`, `sed -n`, `head`, `tail`) on specific line ranges — raw trace logs can exceed 1M characters.
- Focus on the first error in each log — that is the root cause. Later errors are cascading failures.
- Common error types:
  - **Build failures**: Look for the compiler/build tool error before the generic "Failed to build" wrapper.
  - **Dependency resolution**: The deepest package in the chain is the actual failure, not the top-level package.
  - **Upload failures**: Usually transient (network/registry) or metadata issues -- see SKILL.md step 6 for transient classification.
  - **Timeouts**: Check job duration vs. typical duration. Look for hanging operations.
- When quoting log lines in section files, redact credentials, tokens, passwords, and API keys. Replace the value with `[REDACTED]`. Common indicators: `password=`, `token=`, `secret=`, `Bearer `, credential-like strings in URLs.

### Feedback Guidelines

Two categories of observations:

- **Analysis process**: documentation gaps, unavailable resources, missing error patterns, analysis obstacles
- **Source repository**: process patterns that contributed to the failure — duplicated configuration that drifts, missing regression tests, hardcoded values that should be shared constants

Frame each observation as what you expected vs what you encountered, and what gap it reveals. Prioritize observations that would recur across many analyses over one-off issues.

When you find yourself thinking "this failure is straightforward, there's nothing to observe" — review the minimum-bar patterns below. A simple diagnosis can still reveal a process gap.

**Minimum-bar patterns** — always warrant an observation, even when the diagnosis is straightforward:

- A new upstream dependency or version caused the failure (gap: no automated detection)
- A package changed its distribution format without warning
- Configuration exists for a parent package but misses its dependencies
- A settings/config value with no automated validation (typos, template syntax errors)

### Finding Example

```json
{
  "group_id": "<group-directory-name>",
  "title": "<Descriptive title — what failed and the key symptom>",
  "collections": ["<collection-name>"],
  "actions": ["<pipeline-action>"],
  "error_summary": "<One-line error suitable for Slack/Jira preview>",
  "suggested_resolution_summary": "<One-line fix summary>",
  "has_resolution_file": true,
  "confidence": "high",
  "confidence_justification": "<Evidence: log lines, cross-job consistency, or patterns that support the confidence level>",
  "cascade": false,
  "transient": false,
  "target_repo": "https://gitlab.com/<project-path>",
  "target_repo_reason": "<Why this repo — which files need changing and why they live there>",
  "group_consistency": "consistent",
  "feedback_status": "included",
  "references": [
    {"path": "<canonical/path/to/file>", "description": "<How this file was used and what insight it provided>"},
    {"path": "<canonical/path/to/another-file>", "description": "<How this file was used>"}
  ],
  "resources_used": {
    "agent_docs": [{"name": "<relevant-doc.md>", "description": "<How this doc was used and what insight it provided>"}],
    "skills": [],
    "tools": []
  }
}
```

### Resources Used Guidelines

Record resource usage in `finding.json` `resources_used`. Each entry is an object with `name` and `description`. The description should explain how the resource was used and what insight it provided — this appears in the final report. Include resources that were consulted but turned out unhelpful — that feedback is equally valuable for improving workspace documentation. Use empty arrays `[]` when nothing was used for that category.

- **`agent_docs`**: Docs from `agent-docs/` that you read during the investigation. `name`: filename only. `description`: what context or guidance it provided.
- **`skills`**: Skills you loaded during the investigation. `name`: skill name. `description`: what it was used for and what it accomplished.
- **`tools`**: Tools and MCP servers used for purposes beyond what this prompt explicitly instructed. Baseline usage (e.g., `glab` for the trace download and `clean-log.py` commands provided in SKILL.md) is expected and not interesting to report. Report novel usage — additional API calls, exploratory queries, or tools used on your own initiative.
