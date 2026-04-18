"""Registry — generate minions-suite projects.yaml.

'Allow me to compile this installation's records.' — 127 Guilty Spark
"""
from __future__ import annotations

import yaml

from spark.detector import DetectedProject

# Service name prefixes -> service key in the registry
_SERVICE_PREFIXES: dict[str, str] = {
    "svc-": "api",
    "lib-": "lib",
    "tf-": "infra",
    "dbt-": "dbt",
    "job-": "job",
    "tool-": "tool",
    "ui-": "app",
}


def build_registry(
    projects: list[DetectedProject],
    gitlab_url: str,
    model: str = "claude-sonnet-4-6",
    exclude_archived: bool = True,
) -> dict:
    """Build a minions-suite compatible registry dict."""
    registry: dict = {
        "defaults": {
            "model": model,
            "git_provider": "gitlab",
            "gitlab_url": gitlab_url,
        },
        "projects": {},
    }

    for p in projects:
        if exclude_archived and p.archived:
            continue
        if not p.project_id:
            continue

        service_name = _infer_service_name(p.name)

        entry: dict = {
            "project_id": p.project_id,
            "gitlab_url": p.gitlab_url,
            "review_profile": {
                "roles": p.roles,
                "languages": p.languages,
            },
            "issues": {
                "enabled": False,
                "label": "minions",
            },
            "services": {
                service_name: {
                    "project_id": p.project_id,
                    "git_provider": "gitlab",
                    "gitlab_url": p.gitlab_url,
                    "clone_url": p.clone_url,
                    "ci_type": p.ci_type,
                    "deploy_target": p.deploy_target,
                    "language": p.primary_language,
                    "framework": p.framework,
                    "test_command": p.test_command,
                    "lint_command": p.lint_command,
                    "default_branch": p.default_branch,
                },
            },
        }
        registry["projects"][p.name] = entry

    return registry


def _infer_service_name(repo_name: str) -> str:
    """Infer a service name from the repo name."""
    for prefix, name in _SERVICE_PREFIXES.items():
        if repo_name.startswith(prefix):
            return name
    return "main"


def serialize_registry(registry: dict) -> str:
    """Serialize registry to YAML string."""
    return yaml.dump(
        registry,
        default_flow_style=None,
        sort_keys=False,
        allow_unicode=True,
    )
