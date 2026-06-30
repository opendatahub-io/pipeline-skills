# pipeline-skills

Agentic-ci skills for AIPCC pipeline failure analysis — error grouping and root cause analysis.

## Overview

Analysis runs in two stages:

1. **Grouping** — The orchestrator feeds preprocessed job error logs and a job manifest into `pipeline-grouping`. The skill reads all error files, identifies distinct failure patterns across jobs, and groups them by shared root cause. Jobs with the same underlying error are merged into a single group even when they span different collections or pipeline actions. The output is `grouping.json` — a list of error groups with summaries, job IDs, and representative error messages.

   ```
   /workspace/
   ├── _context/
   │   └── grouping-context.json        # job manifest, expected IDs, Jira URL
   ├── jobs/
   │   ├── 101-build-torch/
   │   │   └── errors.txt               # preprocessed error output
   │   ├── 102-build-vllm/
   │   │   └── errors.txt
   │   └── 103-test-integration/
   │       └── errors.txt
   ├── recent-tickets.json              # open Jira tickets for dedup (optional)
   │
   │  ── outputs ──
   ├── grouping.json                    # final grouped failures
   └── dedup-results.json               # Jira ticket matches (if any)
   ```

2. **Root Cause Analysis** — For each error group, `pipeline-rca` investigates trace logs, source code, and build artifacts to diagnose _why_ the failure occurred. It produces structured section files (error overview, root cause diagnosis, suggested resolution) and a `finding.json` with classification metadata, confidence level, target repository for the fix, and references to all files consulted.

   ```
   /workspace/
   ├── _context/
   │   └── rca-context.json             # group metadata, pipeline info, file paths
   ├── _repos/                          # shallow clones for git investigation
   ├── pipeline-context.json            # pipeline metadata
   ├── groups/
   │   └── 01-torch-build-failure/
   │       ├── jobs/
   │       │   ├── 101-build-torch/
   │       │   │   ├── trace.log        # full job trace log
   │       │   │   └── errors.txt       # preprocessed errors
   │       │   └── 102-build-vllm/
   │       │       ├── trace.log
   │       │       └── errors.txt
   │       │
   │       │  ── outputs ──
   │       ├── finding.json             # structured classification + confidence
   │       └── sections/
   │           ├── error-overview.md    # what happened (quoted errors)
   │           ├── root-cause.md        # why it happened (diagnosis)
   │           ├── resolution.md        # how to fix it (optional)
   │           └── feedback.md          # process observations (optional)
   ```

The orchestrator assembles these outputs into per-group reports and routes them to Jira and Slack.

This plugin is the inner layer of the pipeline failure analysis system — the skills that run inside the Claude Code container. The outer layer (Python orchestration, GitLab CI, report assembly, Jira/Slack notifications) lives in [pipeline-failure-analyzer](https://github.com/opendatahub-io/pipeline-failure-analyzer) and [agentic-ci](https://github.com/opendatahub-io/agentic-ci) (generic CI framework).

### Skills

| Skill | Description |
|-------|-------------|
| `pipeline-grouping` | Groups failed pipeline jobs by error similarity using log analysis and Jira ticket deduplication |
| `pipeline-rca` | Root cause analysis on a single error group, producing structured findings with error overview, diagnosis, and resolution guidance |

## License

Apache-2.0
