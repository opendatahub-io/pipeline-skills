---
name: wheel-failure-triage
description: >-
  Deterministically collect Fromager wheel failures across GitLab pipeline
  descendants, create one audit child job, and hand the report to PFA.
allowed-tools: Bash Read
metadata:
  author: ODH
  version: "1.0"
  tags: pipeline, wheels, failure-analysis, ci
---

# Wheel Failure Triage

Use the bundled CLI to inventory wheel build failures and prepare a single audit child pipeline. Keep collection and notification deterministic; PFA handles later analysis and reporting. Do not group failures with an LLM or create a child job per package or failure group.

## Commands

Run the collector from the pipeline workspace:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/wheel_failure_triage.py" prepare --work-dir .wheel-triage
```

For offline runs, pass a normalized JSON file with `--failures <path>`. The report-level `pipeline_url` remains the origin pipeline. Each failure retains its `source_pipeline_url`, which identifies the pipeline that produced it.

The collector writes `.wheel-triage/failures.json`, `.wheel-triage/audit.yml`, and a copy of `wheel_failure_triage.py`. The generated child has one audit job and runs:

```bash
python3 .wheel-triage/wheel_failure_triage.py audit .wheel-triage/failures.json
```

The audit command uses only the Python standard library, prints aggregate counts, and exits nonzero when the report contains failures. It keeps failure messages, URLs, and arbitrary payload fields out of the CI log; PFA reads the complete JSON artifact. Restrict collector and audit artifact access to the authorized CI/PFA readers because the JSON contains unredacted evidence. The audit runs directly from the collector artifact without cloning this repository or installing helper dependencies.

`--work-dir` must be relative to the project workspace and cannot contain `..` or resolve outside it. Generated script and report paths follow that directory, including paths with spaces. Keep the default `.wheel-triage` for the current PFA artifact-reader contract; other directories require the consumer to read the corresponding report path. The audit image is pinned to an immutable UBI Python 3.12 manifest digest.

After the audit bridge reaches a terminal state, the CI notification job invokes:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/wheel_failure_triage.py" notify --work-dir .wheel-triage
```

The CLI starts the PFA pipeline using `BOT_PAT` and forwards the selected pipeline URL. Jira defaults to the `RHAI` project. Notifications are enabled by default: PFA uses its own `JIRA_API_TOKEN` and configured Slack webhook. A nonempty caller `SLACK_WEBHOOK_URL` overrides that destination; an unset or empty value leaves PFA's default intact. Only explicit `PFA_DISABLE_NOTIFICATIONS=true` suppresses Jira and Slack by sending empty downstream credentials.

## Runtime requirements

`prepare` and `notify` require Python packages `requests` and `PyYAML`. Both require `CI_API_V4_URL`, `CI_PROJECT_ID`, and `BOT_PAT`. `prepare` also requires `CI_PIPELINE_ID` and `CI_PIPELINE_URL`; `CI_JOB_ID` is used to identify a collection error. `notify` uses `CI_COMMIT_REF_NAME` and the CI pipeline metadata. Jira and Slack values are optional and may be supplied through `JIRA_PROJECT`, `JIRA_LABELS`, `JIRA_COMPONENTS`, `JIRA_SUMMARY_PREFIX` and `SLACK_WEBHOOK_URL`. Configure the Jira API token in the PFA project, not the caller.

Collection walks upstream root and build child pipelines through GitLab bridge jobs, uses latest job attempts, and checks successful bootstrap reports as well as failed ones. It follows a shared descendant once, excludes the audit and notification flow, and routes artifact requests by the producing job's project ID. If a downstream project cannot be determined, retain the explicit evidence error rather than attributing its artifacts to the root project.
