"""Collect wheel failures, create one audit child, and invoke PFA."""

import json
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import requests
import yaml

PFA_PROJECT: str = "redhat/rhel-ai/agentic-ci/pipeline-failure-analyzer"
FAILURES_PATH: str = "mnt/work-dir/partial-failures.json"
BUILD_ORDER_PATH: str = "mnt/work-dir/build-order.json"
TERMINAL: frozenset[str] = frozenset({"success", "failed", "canceled", "skipped"})
AUDIT_JOB: str = "wheel-failure-audit"
AUDIT_IMAGE: str = (
    "registry.access.redhat.com/ubi9/python-312@"
    "sha256:56fad467cb1e41666f0028b7fd71107df0556bcc9ecfc577597858c85618f55c"
)
LAUNCH_JOB: str = "launch-wheel-audit"
TRIAGE_JOB: str = "collect-wheel-failures"
AUDIT_FLOW_JOBS: frozenset[str] = frozenset(
    {AUDIT_JOB, LAUNCH_JOB, TRIAGE_JOB, "analyze-failures"}
)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class GitLabSession(requests.Session):
    """Artifact downloads may redirect to storage outside GitLab."""

    def rebuild_auth(
        self, prepared_request: requests.PreparedRequest, response: requests.Response
    ) -> None:
        super().rebuild_auth(prepared_request, response)
        if self.should_strip_auth(response.request.url, prepared_request.url):
            prepared_request.headers.pop("PRIVATE-TOKEN", None)


class GitLab:
    """Small API client; listing and polling require an API access token."""

    def __init__(self, environ: dict[str, str]) -> None:
        self.api_url: str = environ["CI_API_V4_URL"].rstrip("/")
        self.project_id: str = environ["CI_PROJECT_ID"]
        self.session: requests.Session = GitLabSession()
        self.session.headers["PRIVATE-TOKEN"] = environ["BOT_PAT"]

    def get(
        self, path: str, *, project_id: int | str | None = None, **params: Any
    ) -> requests.Response:
        project: str = quote(str(project_id or self.project_id), safe="")
        response: requests.Response = self.session.get(
            f"{self.api_url}/projects/{project}/{path}",
            params=params,
            timeout=60,
        )
        response.raise_for_status()
        return response

    def list(
        self, path: str, *, project_id: int | str | None = None, **params: Any
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page: str = "1"
        while page:
            response: requests.Response = self.get(
                path, project_id=project_id, page=page, per_page=100, **params
            )
            items.extend(response.json())
            page = response.headers.get("X-Next-Page", "")
        return items

    def artifact(
        self, job_id: int, path: str, *, project_id: int | str | None = None
    ) -> bytes | None:
        try:
            return self.get(
                f"jobs/{job_id}/artifacts/{quote(path, safe='/')}",
                project_id=project_id,
            ).content
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise

    def get_project_metadata(self, project_id: int | str) -> dict[str, Any]:
        response: requests.Response = self.session.get(
            f"{self.api_url}/projects/{quote(str(project_id), safe='')}", timeout=60
        )
        response.raise_for_status()
        return response.json()


def job_context(name: str) -> dict[str, str]:
    match: re.Match[str] | None = re.fullmatch(
        r"(?P<collection>.+)-(?P<channel>(?:cpu|cuda|rocm|gaudi|neuron|tpu|spyre|rubin)"
        r"[^-]*(?:-torch[^-]+)?-(?:ubi[0-9.]+|el[0-9.]+|hb))-"
        r"(?P<arch>aarch64|x86_64|ppc64le|s390x)-(?:bootstrap-and-onboard|build-wheels)",
        name,
    )
    if match is None:
        return {}
    context: dict[str, str] = match.groupdict()
    context["variant"] = re.sub(r"-torch[^-]+", "", context["channel"])
    return context


def record(
    job: dict[str, Any],
    key: str,
    kind: str,
    message: str,
    source_pipeline_url: str,
) -> dict[str, Any]:
    return {
        "id": f"{job['id']}:{key}",
        "kind": kind,
        "name": job["name"],
        "version": None,
        "phase": job.get("stage", ""),
        "error_type": (job.get("failure_reason") or job["status"])
        if kind == "job"
        else kind,
        "message": message,
        "source_pipeline_url": source_pipeline_url,
        "producer": {
            **job_context(job["name"]),
            "job_id": job["id"],
            "job_name": job["name"],
            "job_url": job["web_url"],
            "status": job["status"],
        },
    }


def wheel_record(
    job: dict[str, Any],
    failure: Any,
    index: int,
    source_pipeline_url: str,
) -> dict[str, Any]:
    if not isinstance(failure, dict) or any(
        not isinstance(failure.get(field), str)
        or (field != "message" and not failure[field])
        for field in ("name", "phase", "error_type", "message")
    ):
        raise ValueError("Invalid Fromager failure record")
    name: str = failure["name"]
    version: Any = failure.get("version")
    if version is not None and not isinstance(version, str):
        raise ValueError("Failure version must be a string or null")
    if "==" in name:
        embedded: str
        name, embedded = name.rsplit("==", 1)
        if version is not None and version != embedded:
            raise ValueError("Failure versions disagree")
        version = embedded
    entry: dict[str, Any] = record(
        job,
        str(index),
        "wheel",
        failure["message"],
        source_pipeline_url,
    )
    entry.update(
        name=re.sub(r"[-_.]+", "-", name).lower(),
        version=version,
        phase=failure["phase"],
        error_type=failure["error_type"],
    )
    return entry


def producer_records(
    job: dict[str, Any],
    raw: bytes | None,
    source_pipeline_url: str,
) -> list[dict[str, Any]]:
    """Normalize the original Fromager report fetched from its bootstrap job."""
    try:
        if raw is None:
            return []
        data: Any = json.loads(raw)
        if not isinstance(data, dict):
            raise TypeError("Failure report must be an object")
        failures: Any = data.get("failures")
        if not isinstance(failures, list):
            raise TypeError("Failure report must contain a failures array")
        result: list[dict[str, Any]] = []
        for index, failure in enumerate(failures):
            try:
                result.append(wheel_record(job, failure, index, source_pipeline_url))
            except ValueError as exc:
                entry: dict[str, Any] = record(
                    job,
                    f"{index}:evidence",
                    "evidence",
                    str(exc),
                    source_pipeline_url,
                )
                entry["diagnostics"] = json.dumps(failure)
                result.append(entry)
        return result
    except (TypeError, ValueError, UnicodeError) as exc:
        evidence: dict[str, Any] = record(
            job, "evidence", "evidence", str(exc), source_pipeline_url
        )
        if raw:
            evidence["diagnostics"] = raw[-20000:].decode("utf-8", errors="replace")
        return [evidence]


def project_url_from_job(job_url: str) -> str | None:
    """Return a trusted GitLab project URL extracted from a job URL."""
    parsed = urlsplit(job_url)
    match: re.Match[str] | None = re.fullmatch(r"(.+)/-/jobs/[0-9]+/?", parsed.path)
    if match is None or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, match.group(1), "", ""))


def project_url_from_pipeline(pipeline_url: str) -> str | None:
    parsed = urlsplit(pipeline_url)
    match: re.Match[str] | None = re.fullmatch(
        r"(.+)/-/pipelines/[0-9]+/?", parsed.path
    )
    if match is None or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, match.group(1), "", ""))


def pipeline_url_from_project(project_url: str, pipeline_id: int | str) -> str:
    if not str(pipeline_id).isdecimal():
        raise ValueError(f"Pipeline ID must be numeric: {pipeline_id}")
    return f"{project_url.rstrip('/')}/-/pipelines/{pipeline_id}"


def pipeline_evidence(
    pipeline_id: int | str,
    project_id: int | str,
    source_pipeline_url: str,
    message: str,
    *,
    occurrence_key: str = "inventory",
) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": pipeline_id,
        "name": "pipeline-inventory",
        "web_url": source_pipeline_url,
        "status": "failed",
        "stage": "inventory",
        "project_id": project_id,
    }
    return record(
        job,
        occurrence_key,
        "evidence",
        f"Could not inventory project {project_id}, pipeline {pipeline_id}: {message}",
        source_pipeline_url,
    )


def latest_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the latest retry attempt for each logical GitLab job."""
    latest: dict[str, dict[str, Any]] = {}
    for job in jobs:
        if job.get("retried", False):
            continue
        name: str = str(job["name"])
        previous: dict[str, Any] | None = latest.get(name)
        if previous is None or int(job["id"]) > int(previous["id"]):
            latest[name] = job
    return list(latest.values())


def source_url_for_pipeline(
    client: GitLab,
    pipeline_id: int | str,
    project_id: int | str,
    *,
    known_url: str | None,
    jobs: list[dict[str, Any]],
    root_pipeline_id: int | str,
    root_project_id: int | str,
    root_pipeline_url: str,
) -> str:
    if known_url:
        return known_url
    if (
        root_pipeline_url
        and str(pipeline_id) == str(root_pipeline_id)
        and str(project_id) == str(root_project_id)
    ):
        return root_pipeline_url
    try:
        metadata: dict[str, Any] = client.get(
            f"pipelines/{pipeline_id}", project_id=project_id
        ).json()
        url: Any = metadata.get("web_url")
        if isinstance(url, str) and url:
            return url
    except (requests.RequestException, ValueError):
        pass
    for job in jobs:
        project_url: str | None = project_url_from_job(str(job.get("web_url", "")))
        if project_url:
            return pipeline_url_from_project(project_url, pipeline_id)
    if str(pipeline_id) == str(root_pipeline_id) and str(project_id) == str(
        root_project_id
    ):
        raise ValueError("Root pipeline URL is unavailable")
    try:
        project: dict[str, Any] = client.get_project_metadata(project_id)
        project_url = project.get("web_url")
        if isinstance(project_url, str) and project_url:
            return pipeline_url_from_project(project_url, pipeline_id)
    except (requests.RequestException, ValueError):
        pass
    raise ValueError(
        f"Pipeline {pipeline_id} in project {project_id} has no trusted pipeline URL"
    )


def collect(
    client: GitLab,
    pipeline_id: int | str,
    *,
    require_producers: bool = True,
    root_project_id: int | str | None = None,
    root_pipeline_url: str | None = None,
    current_job_id: int | str | None = None,
) -> list[dict[str, Any]]:
    root_project: int | str = root_project_id or client.project_id
    root_url: str = root_pipeline_url or ""
    if not root_url:
        raise ValueError("Root pipeline URL is required for failure provenance")
    failures: list[dict[str, Any]] = []
    producers: int = 0
    pending: list[tuple[int | str, int | str, str | None]] = [
        (root_project, pipeline_id, root_url)
    ]
    visited: set[tuple[str, str]] = set()

    while pending:
        project_id, current_pipeline_id, known_url = pending.pop(0)
        identity: tuple[str, str] = (str(project_id), str(current_pipeline_id))
        if identity in visited:
            continue
        visited.add(identity)
        source_pipeline_url: str = known_url or ""
        if not source_pipeline_url and identity == (
            str(root_project),
            str(pipeline_id),
        ):
            source_pipeline_url = root_url
        if not source_pipeline_url:
            try:
                source_pipeline_url = source_url_for_pipeline(
                    client,
                    current_pipeline_id,
                    project_id,
                    known_url=None,
                    jobs=[],
                    root_pipeline_id=pipeline_id,
                    root_project_id=root_project,
                    root_pipeline_url=root_url,
                )
            except (requests.RequestException, ValueError, KeyError):
                pass
        try:
            raw_jobs: list[dict[str, Any]] = client.list(
                f"pipelines/{current_pipeline_id}/jobs",
                project_id=project_id,
                include_retried="true",
            )
            jobs: list[dict[str, Any]] = latest_jobs(raw_jobs)
            if not source_pipeline_url:
                source_pipeline_url = source_url_for_pipeline(
                    client,
                    current_pipeline_id,
                    project_id,
                    known_url=None,
                    jobs=jobs,
                    root_pipeline_id=pipeline_id,
                    root_project_id=root_project,
                    root_pipeline_url=root_url,
                )
        except (requests.RequestException, ValueError, KeyError) as exc:
            if source_pipeline_url:
                failures.append(
                    pipeline_evidence(
                        current_pipeline_id, project_id, source_pipeline_url, str(exc)
                    )
                )
            else:
                failures.append(
                    pipeline_evidence(
                        pipeline_id,
                        root_project,
                        root_url,
                        f"Cannot attribute descendant project {project_id}, pipeline "
                        f"{current_pipeline_id}: {exc}",
                        occurrence_key=f"inventory:{project_id}:{current_pipeline_id}",
                    )
                )
            continue

        for job in jobs:
            job["project_id"] = project_id
            if current_job_id is not None and str(job.get("id")) == str(current_job_id):
                continue
            if job.get("stage") in {"wheel-triage", "notify"}:
                continue
            if job.get("name") in AUDIT_FLOW_JOBS:
                continue
            if job["status"] not in TERMINAL and job["status"] != "manual":
                if job.get("stage") == "bootstrap":
                    failures.append(
                        record(
                            job,
                            "completion",
                            "evidence",
                            f"Producer {job['id']} has not finished",
                            source_pipeline_url,
                        )
                    )
                continue
            entries: list[dict[str, Any]] = []
            # Build jobs inherit bootstrap reports and telemetry. Reading only
            # bootstrap artifacts prevents counting copied reports twice.
            if job.get("stage") == "bootstrap":
                producers += 1
                if job["status"] in {"skipped", "manual"}:
                    failures.append(
                        record(
                            job,
                            "completion",
                            "evidence",
                            f"Bootstrap did not run: {job['status']}",
                            source_pipeline_url,
                        )
                    )
                    continue
                try:
                    raw: bytes | None = client.artifact(
                        job["id"], FAILURES_PATH, project_id=project_id
                    )
                    entries = producer_records(job, raw, source_pipeline_url)
                    if raw is None and job["status"] == "success":
                        order: bytes | None = client.artifact(
                            job["id"], BUILD_ORDER_PATH, project_id=project_id
                        )
                        if order is None or not isinstance(json.loads(order), list):
                            entries.append(
                                record(
                                    job,
                                    "completion",
                                    "evidence",
                                    "Successful bootstrap has no valid final build order",
                                    source_pipeline_url,
                                )
                            )
                except requests.RequestException as exc:
                    entries = [
                        record(
                            job,
                            "evidence",
                            "evidence",
                            f"Cannot download bootstrap artifacts: {exc}",
                            source_pipeline_url,
                        )
                    ]
                except (TypeError, ValueError, UnicodeError) as exc:
                    entries = [
                        record(
                            job,
                            "completion",
                            "evidence",
                            f"Invalid bootstrap completion evidence: {exc}",
                            source_pipeline_url,
                        )
                    ]
                if entries:
                    try:
                        log: bytes | None = client.artifact(
                            job["id"], "mnt/errors.log", project_id=project_id
                        )
                        if log:
                            diagnostics: str = log[-20000:].decode(
                                "utf-8", errors="replace"
                            )
                            for entry in entries:
                                entry["producer_diagnostics"] = diagnostics
                    except requests.RequestException as exc:
                        entries.append(
                            record(
                                job,
                                "logs",
                                "evidence",
                                f"Cannot download producer diagnostics: {exc}",
                                source_pipeline_url,
                            )
                        )
            # Retain actual failed/canceled jobs, including failures after a
            # bootstrap report was written.
            if job["status"] in {"failed", "canceled"}:
                entry = record(
                    job,
                    "job",
                    "job",
                    f"Producer job {job['status']}",
                    source_pipeline_url,
                )
                try:
                    entry["diagnostics"] = client.get(
                        f"jobs/{job['id']}/trace", project_id=project_id
                    ).text[-20000:]
                except requests.RequestException as exc:
                    entry["diagnostics_error"] = (
                        f"Cannot download failed job trace: {exc}"
                    )
                entries.append(entry)
            failures.extend(entries)

        try:
            bridges: list[dict[str, Any]] = client.list(
                f"pipelines/{current_pipeline_id}/bridges", project_id=project_id
            )
        except requests.RequestException as exc:
            failures.append(
                pipeline_evidence(
                    current_pipeline_id,
                    project_id,
                    source_pipeline_url,
                    f"Cannot list downstream pipelines: {exc}",
                )
            )
            continue
        for bridge in bridges:
            if bridge.get("stage") in {"wheel-triage", "notify"}:
                continue
            if bridge.get("name") in AUDIT_FLOW_JOBS:
                continue
            downstream: Any = bridge.get("downstream_pipeline")
            if not isinstance(downstream, dict) or downstream.get("id") is None:
                continue
            child_id: int | str = downstream["id"]
            child_project_id: int | str | None = downstream.get("project_id")
            child_url: Any = downstream.get("web_url")
            if child_project_id is None:
                child_project_url: str | None = (
                    project_url_from_pipeline(child_url)
                    if isinstance(child_url, str)
                    else None
                )
                parent_project_url: str | None = project_url_from_pipeline(
                    source_pipeline_url
                )
                if child_project_url and child_project_url == parent_project_url:
                    child_project_id = project_id
                else:
                    evidence_url: str = (
                        child_url
                        if isinstance(child_url, str) and child_url
                        else source_pipeline_url
                    )
                    failures.append(
                        pipeline_evidence(
                            child_id,
                            "unknown",
                            evidence_url,
                            "Downstream pipeline project_id cannot be determined; "
                            "artifact project cannot be determined",
                        )
                    )
                    continue
            pending.append((child_project_id, child_id, child_url))

    if require_producers and not producers and not failures:
        raise ValueError("Nightly pipeline contains no wheel producers")
    return failures


def prepare(
    work_dir: Path, environ: dict[str, str], failures_file: Path | None = None
) -> None:
    project_root: Path = Path.cwd().resolve()
    if work_dir.is_absolute() or ".." in work_dir.parts:
        raise ValueError(
            "Work directory must be project-relative and cannot contain '..'"
        )
    try:
        work_dir.resolve().relative_to(project_root)
    except ValueError as exc:
        raise ValueError(
            "Work directory must stay within the project workspace"
        ) from exc

    report_file: Path = work_dir.joinpath("failures.json")
    cli_file: Path = work_dir.joinpath("wheel_failure_triage.py")
    config_file: Path = work_dir.joinpath("audit.yml")
    for output_path in (report_file, cli_file, config_file):
        try:
            output_path.resolve().relative_to(project_root)
        except ValueError as exc:
            raise ValueError(
                "Generated paths must stay within the project workspace"
            ) from exc
    report_path: str = report_file.as_posix()
    cli_path: str = cli_file.as_posix()

    if failures_file:
        failures: list[dict[str, Any]] = read_json(failures_file)["failures"]
    else:
        try:
            failures = collect(
                GitLab(environ),
                environ["CI_PIPELINE_ID"],
                require_producers=environ.get("CI_PIPELINE_SOURCE") == "schedule",
                root_project_id=environ["CI_PROJECT_ID"],
                root_pipeline_url=environ["CI_PIPELINE_URL"],
                current_job_id=environ.get("CI_JOB_ID"),
            )
        except (requests.RequestException, ValueError, KeyError) as exc:
            failures = [
                record(
                    {
                        "id": environ["CI_JOB_ID"],
                        "name": "failure-inventory",
                        "web_url": environ["CI_PIPELINE_URL"],
                        "status": "failed",
                        "stage": "wheel-triage",
                        "project_id": environ["CI_PROJECT_ID"],
                    },
                    "inventory",
                    "evidence",
                    f"Could not collect pipeline failures: {exc}",
                    environ["CI_PIPELINE_URL"],
                )
            ]
    if len({entry["id"] for entry in failures}) != len(failures):
        raise ValueError("Failure occurrence IDs must be unique")
    records: list[dict[str, Any]] = []
    producer_logs: list[dict[str, Any]] = []
    seen_logs: set[tuple[str, str]] = set()
    for failure in failures:
        entry: dict[str, Any] = dict(failure)
        entry.setdefault("source_pipeline_url", environ["CI_PIPELINE_URL"])
        diagnostics: str = entry.pop("producer_diagnostics", "")
        if diagnostics:
            producer: dict[str, Any] = entry["producer"]
            key: tuple[str, str] = (str(producer["job_id"]), diagnostics)
            if key not in seen_logs:
                producer_logs.append({"producer": producer, "log": diagnostics})
                seen_logs.add(key)
        records.append(entry)
    write_json(
        report_file,
        {
            "pipeline_url": environ["CI_PIPELINE_URL"],
            "failures": records,
            "producer_logs": producer_logs,
        },
    )
    config: dict[str, Any] = {
        "workflow": {
            "auto_cancel": {"on_job_failure": "none", "on_new_commit": "none"}
        },
        "stages": ["audit"],
        AUDIT_JOB: {
            "stage": "audit",
            "image": AUDIT_IMAGE,
            "tags": ["aipcc-small-x86_64"],
            "rules": [{"if": '$CI_PIPELINE_SOURCE == "parent_pipeline"'}],
            "inherit": {"variables": False},
            "interruptible": False,
            "needs": [
                {
                    "pipeline": str(environ["CI_PIPELINE_ID"]),
                    "job": TRIAGE_JOB,
                }
            ],
            "script": [
                f"python3 {shlex.quote(cli_path)} audit {shlex.quote(report_path)}"
            ],
            "artifacts": {
                "paths": [report_path],
                "when": "always",
                "expire_in": "7 days",
            },
        },
    }
    cli_source: Path = Path(__file__).with_name("wheel_failure_triage.py")
    shutil.copyfile(cli_source, cli_file)
    config_file.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    print(f"Collected {len(failures)} failures into one audit report")


def wait_for_audit(
    client: GitLab, environ: dict[str, str], timeout: float = 1200
) -> tuple[str, bool]:
    """Wait outside the child so PFA receives its completed failure report."""
    deadline: float = time.monotonic() + timeout
    while time.monotonic() < deadline:
        bridges: list[dict[str, Any]] = client.list(
            f"pipelines/{environ['CI_PIPELINE_ID']}/bridges"
        )
        matches: list[dict[str, Any]] = [
            bridge for bridge in bridges if bridge["name"] == LAUNCH_JOB
        ]
        if len(matches) != 1:
            raise ValueError("Expected exactly one audit launcher")
        bridge: dict[str, Any] = matches[0]
        child: dict[str, Any] | None = bridge.get("downstream_pipeline")
        if child:
            pipeline: dict[str, Any] = client.get(f"pipelines/{child['id']}").json()
            if pipeline["status"] in TERMINAL:
                if pipeline["status"] not in {"success", "failed"}:
                    raise ValueError(
                        f"Audit child did not complete: {pipeline['status']}"
                    )
                return pipeline["web_url"], pipeline["status"] == "success"
        elif bridge["status"] in {"failed", "canceled", "skipped"}:
            # Collection or child creation failed: analyze the parent instead.
            return environ["CI_PIPELINE_URL"], False
        time.sleep(10)
    raise TimeoutError("Timed out waiting for the audit child")


def notify(client: GitLab, environ: dict[str, str], work_dir: Path) -> None:
    pipeline_url: str
    clean: bool
    pipeline_url, clean = wait_for_audit(client, environ)
    nightly: bool = environ.get("CI_PIPELINE_SOURCE") == "schedule"
    prefix: str = "Nightly" if nightly else "Post-Merge Failure"
    variables: dict[str, str] = {
        "PIPELINE_URL": pipeline_url,
        "JIRA_PROJECT": environ.get("JIRA_PROJECT") or "RHAI",
        "JIRA_LABELS": environ.get(
            "JIRA_LABELS", "nightly-pipeline" if nightly else "builder-pipeline"
        ),
        "JIRA_COMPONENTS": environ.get("JIRA_COMPONENTS", "AIPCC Ecosystems"),
        "JIRA_SUMMARY_PREFIX": environ.get(
            "JIRA_SUMMARY_PREFIX", f"[{prefix}] - {environ['CI_COMMIT_REF_NAME']}"
        ),
        "NOTIFY_ON_SUCCESS": "true" if clean else "false",
        "DATADOG_CONFIG": environ.get(
            "DATADOG_CONFIG", "datadog:\n  tags:\n    service: aipcc-fondue\n"
        ),
    }
    webhook: str = environ.get("SLACK_WEBHOOK_URL", "").strip()
    if webhook:
        variables["SLACK_WEBHOOK_URL"] = webhook
    if environ.get("PFA_DISABLE_NOTIFICATIONS") == "true":
        variables.update(JIRA_API_TOKEN="", SLACK_WEBHOOK_URL="")
    response: requests.Response = client.session.post(
        f"{client.api_url}/projects/{quote(PFA_PROJECT, safe='')}/pipeline",
        json={
            "ref": environ.get("PFA_REF", "main"),
            "variables": [
                {"key": key, "value": value} for key, value in variables.items()
            ],
        },
        timeout=60,
    )
    response.raise_for_status()
    result: dict[str, Any] = response.json()
    write_json(work_dir.joinpath("pfa-pipeline.json"), result)
    print(f"PFA pipeline: {result['web_url']}")
