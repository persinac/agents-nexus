"""Detector — infer project characteristics for registry generation.

'A thorough scan of this installation reveals...' — 127 Guilty Spark
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spark.github import GitHubProject
    from spark.gitlab import GitLabProject

    VCSProject = GitLabProject | GitHubProject

# GitLab language names -> minions-compatible names
_LANGUAGE_MAP: dict[str, str | None] = {
    "Python": "python",
    "TypeScript": "typescript",
    "JavaScript": "typescript",
    "Go": "go",
    "HCL": "terraform",
    "Shell": "shell",
    "SQL": "sql",
    "Dockerfile": None,
    "Makefile": None,
    "HTML": None,
    "CSS": None,
    "SCSS": None,
    "YAML": None,
    "JSON": None,
    "Markdown": None,
}

# Language -> default review roles
_ROLE_MAP: dict[str, list[str]] = {
    "python": ["backend"],
    "go": ["backend"],
    "typescript": ["backend"],
    "terraform": ["devops"],
    "sql": ["data_engineer"],
    "shell": ["devops"],
}


@dataclass
class DetectedProject:
    """Auto-detected characteristics of an installation."""

    name: str
    project_id: str
    clone_url: str
    gitlab_url: str
    default_branch: str
    primary_language: str
    languages: list[str]
    roles: list[str]
    framework: str
    ci_type: str
    deploy_target: str
    test_command: str
    lint_command: str
    archived: bool
    services: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "DetectedProject":
        """Reconstruct from a persisted dict (installations.json `detected` blob).

        Tolerant of missing keys so an older/partial metadata record still
        yields a usable project (empty strings / lists for anything absent).
        """
        return cls(
            name=data.get("name", ""),
            project_id=data.get("project_id", ""),
            clone_url=data.get("clone_url", ""),
            gitlab_url=data.get("gitlab_url", ""),
            default_branch=data.get("default_branch", ""),
            primary_language=data.get("primary_language", ""),
            languages=list(data.get("languages") or []),
            roles=list(data.get("roles") or []),
            framework=data.get("framework", ""),
            ci_type=data.get("ci_type", ""),
            deploy_target=data.get("deploy_target", ""),
            test_command=data.get("test_command", ""),
            lint_command=data.get("lint_command", ""),
            archived=bool(data.get("archived", False)),
            services=list(data.get("services") or []),
        )


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(errors="replace"))
    except Exception:
        return None


def _read_toml_raw(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except Exception:
        return None


def _read_gomod(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except Exception:
        return None


def detect_clone_url(repo_dir: Path) -> str:
    """Get SSH clone URL from git remote."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        url = result.stdout.strip()
    except Exception:
        return ""

    if not url:
        return ""

    # Already SSH format
    if url.startswith("git@"):
        return url

    # Convert HTTPS to SSH: https://gitlab.com/group/repo.git -> git@gitlab.com:group/repo.git
    match = re.match(r"https?://([^/]+)/(.+?)(?:\.git)?$", url)
    if match:
        host, path = match.group(1), match.group(2)
        return f"git@{host}:{path}.git"

    return url


def detect_language(
    gitlab_project: VCSProject | None, repo_dir: Path
) -> tuple[str, list[str]]:
    """Determine primary language and all languages.

    Returns (primary_language, [all_languages]) using minions-compatible names.
    """
    lang_pcts: dict[str, float] = {}

    if gitlab_project and gitlab_project.languages:
        lang_pcts = gitlab_project.languages
    else:
        # Fallback: detect from file presence
        if (repo_dir / "pyproject.toml").exists() or (repo_dir / "setup.py").exists():
            lang_pcts["Python"] = 100.0
        elif (repo_dir / "package.json").exists():
            lang_pcts["TypeScript"] = 100.0
        elif (repo_dir / "go.mod").exists():
            lang_pcts["Go"] = 100.0
        elif (repo_dir / "main.tf").exists():
            lang_pcts["HCL"] = 100.0

    # Normalize to minions names, filter out non-review languages
    normalized: list[tuple[str, float]] = []
    for lang, pct in sorted(lang_pcts.items(), key=lambda x: -x[1]):
        mapped = _LANGUAGE_MAP.get(lang)
        if mapped and mapped not in [n for n, _ in normalized]:
            normalized.append((mapped, pct))

    if not normalized:
        return ("", [])

    primary = normalized[0][0]
    all_langs = [n for n, _ in normalized]

    # dbt projects are primarily SQL regardless of what GitLab reports
    if (repo_dir / "dbt_project.yml").exists():
        primary = "sql"
        if "sql" not in all_langs:
            all_langs.insert(0, "sql")

    return (primary, all_langs)


def detect_roles(
    primary_language: str, languages: list[str], repo_dir: Path
) -> list[str]:
    """Infer review roles from languages and project structure."""
    if not primary_language:
        return []

    roles = list(_ROLE_MAP.get(primary_language, ["backend"]))

    # Frontend override: check for React/Angular/Vue in package.json
    if primary_language == "typescript":
        pkg = _read_json(repo_dir / "package.json")
        if pkg:
            all_deps = {
                **pkg.get("dependencies", {}),
                **pkg.get("devDependencies", {}),
            }
            if any(k in all_deps for k in ("react", "next", "@angular/core", "vue")):
                roles = ["frontend"]

    # dbt projects
    if (repo_dir / "dbt_project.yml").exists():
        roles = ["data_engineer"]

    return roles


def detect_framework(primary_language: str, repo_dir: Path) -> str:
    """Detect framework from dependency files."""
    if primary_language == "python":
        toml = _read_toml_raw(repo_dir / "pyproject.toml")
        if toml:
            for fw, name in [
                ("fastapi", "fastapi"),
                ("flask", "flask"),
                ("django", "django"),
                ("airflow", "airflow"),
            ]:
                if fw in toml.lower():
                    return name
        if (repo_dir / "dbt_project.yml").exists():
            return "dbt"

    elif primary_language == "typescript":
        pkg = _read_json(repo_dir / "package.json")
        if pkg:
            all_deps = {
                **pkg.get("dependencies", {}),
                **pkg.get("devDependencies", {}),
            }
            for fw, name in [
                ("next", "nextjs"),
                ("react", "react"),
                ("@nestjs/core", "nestjs"),
                ("fastify", "fastify"),
                ("express", "express"),
            ]:
                if fw in all_deps:
                    return name

    elif primary_language == "go":
        gomod = _read_gomod(repo_dir / "go.mod")
        if gomod:
            for fw, name in [
                ("gin-gonic/gin", "gin"),
                ("labstack/echo", "echo"),
                ("gofiber/fiber", "fiber"),
            ]:
                if fw in gomod:
                    return name

    elif primary_language == "terraform":
        return "terraform"

    if (repo_dir / "dbt_project.yml").exists():
        return "dbt"

    return ""


def detect_ci_type(repo_dir: Path) -> str:
    """Detect CI system from config files."""
    if (repo_dir / ".gitlab-ci.yml").exists():
        return "gitlab_ci"
    if (repo_dir / ".github" / "workflows").is_dir():
        return "github_actions"
    if (repo_dir / "Jenkinsfile").exists():
        return "jenkins"
    return ""


def detect_deploy_target(repo_dir: Path) -> str:
    """Detect deployment target from project structure."""
    name = repo_dir.name

    # Libraries, terraform modules, dbt projects, tools -> none
    for prefix in ("lib-", "tf-", "dbt-", "tool-"):
        if name.startswith(prefix):
            return "none"

    # Check for dbt project
    if (repo_dir / "dbt_project.yml").exists():
        return "none"

    # Kubernetes indicators
    k8s_dirs = ["kubernetes", "k8s", "deploy", "kustomize"]
    for d in k8s_dirs:
        if (repo_dir / d).is_dir():
            return "k8s"
    if (repo_dir / "kustomization.yaml").exists() or (repo_dir / "kustomization.yml").exists():
        return "k8s"
    if (repo_dir / "Chart.yaml").exists() or (repo_dir / "helm").is_dir():
        return "k8s"

    # AppRunner
    if (repo_dir / "apprunner.yaml").exists():
        return "apprunner"

    # Amplify
    if (repo_dir / "amplify").is_dir() or (repo_dir / "amplify.yml").exists():
        return "amplify"

    # Has Dockerfile but no specific deploy config -> assume k8s
    if (repo_dir / "Dockerfile").exists():
        return "k8s"

    return "none"


def detect_test_command(primary_language: str, repo_dir: Path) -> str:
    """Detect test command from project config."""
    if primary_language == "typescript":
        pkg = _read_json(repo_dir / "package.json")
        if pkg:
            scripts = pkg.get("scripts", {})
            if "cov" in scripts:
                return "npm run cov"
            if "test" in scripts:
                return "npm run test"

    elif primary_language == "python":
        toml = _read_toml_raw(repo_dir / "pyproject.toml")
        if toml and ("[tool.pytest" in toml or "pytest" in toml.lower()):
            return "pytest"

    elif primary_language == "go":
        if (repo_dir / "go.mod").exists():
            return "go test ./..."

    if (repo_dir / "dbt_project.yml").exists():
        return "dbt test"

    return ""


def detect_lint_command(primary_language: str, repo_dir: Path) -> str:
    """Detect lint command from project config."""
    if primary_language == "typescript":
        pkg = _read_json(repo_dir / "package.json")
        if pkg:
            scripts = pkg.get("scripts", {})
            if "lint" in scripts:
                return "npm run lint"

    elif primary_language == "python":
        toml = _read_toml_raw(repo_dir / "pyproject.toml")
        if toml and "[tool.ruff" in toml:
            return "ruff check ."

    elif primary_language == "go":
        if (repo_dir / "go.mod").exists():
            return "go vet ./..."

    return ""


# ── Service / technology detection ──────────────────────────────────────
#
# Broad search queries ("cognito", "oauth", "redis") fail when the repo's
# summary text never mentions the service. These tables map dependency names
# and config signals to short technology tags that get embedded into the
# monitor-log summary so both vector and BM25 search can find the repo.

# npm package name (exact or prefix via *) -> comma-separated tags
_NPM_SERVICE_MAP: dict[str, str] = {
    "@aws-sdk/client-cognito-identity-provider": "cognito",
    "amazon-cognito-identity-js": "cognito",
    "@aws-sdk/client-s3": "s3",
    "@aws-sdk/client-dynamodb": "dynamodb",
    "aws-amplify": "amplify",
    "next-auth": "oauth, nextauth",
    "passport": "oauth, passport",
    "jsonwebtoken": "jwt",
    "@auth0/nextjs-auth0": "auth0, oauth",
    "auth0": "auth0, oauth",
    "firebase-admin": "firebase-auth",
    "redis": "redis",
    "ioredis": "redis",
    "@elastic/elasticsearch": "elasticsearch",
    "kafkajs": "kafka",
    "pg": "postgres",
    "knex": "postgres",
    "prisma": "postgres",
    "@prisma/client": "postgres",
    "mongoose": "mongodb",
    "mongodb": "mongodb",
    "googleapis": "google-api",
    "@google-cloud/*": "gcp",
}

# python distribution name -> comma-separated tags (matched as substrings of
# pyproject.toml / requirements.txt, lowercased)
_PY_SERVICE_MAP: dict[str, str] = {
    "boto3": "aws",
    "botocore": "aws",
    "django-allauth": "oauth",
    "authlib": "oauth",
    "python-jose": "jwt",
    "pyjwt": "jwt",
    "celery": "celery",
    "sqlalchemy": "postgres, sql",
    "psycopg": "postgres",
    "redis": "redis",
    "elasticsearch": "elasticsearch",
    "pymongo": "mongodb",
}

# terraform resource prefix -> tag
_TF_RESOURCE_MAP: dict[str, str] = {
    "aws_cognito": "cognito",
    "aws_s3": "s3",
    "aws_dynamodb": "dynamodb",
    "aws_rds": "rds",
    "aws_db_instance": "rds",
    "aws_lambda": "lambda",
    "aws_sqs": "sqs",
    "aws_sns": "sns",
    "aws_elasticache": "redis",
    "aws_kinesis": "kinesis",
    "aws_ecs": "ecs",
    "aws_eks": "eks",
    "aws_apigatewayv2": "api-gateway",
    "aws_api_gateway": "api-gateway",
    "aws_secretsmanager": "secrets-manager",
}

# docker-compose image-name substring -> tag
_COMPOSE_IMAGE_MAP: dict[str, str] = {
    "redis": "redis",
    "postgres": "postgres",
    "elasticsearch": "elasticsearch",
    "opensearch": "elasticsearch",
    "localstack": "aws-local",
    "kafka": "kafka",
    "mongo": "mongodb",
    "clickhouse": "clickhouse",
}


def _services_from_npm(repo_dir: Path) -> set[str]:
    tags: set[str] = set()
    pkg = _read_json(repo_dir / "package.json")
    if not pkg:
        return tags
    all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    for dep in all_deps:
        for pattern, mapped in _NPM_SERVICE_MAP.items():
            if pattern.endswith("*"):
                if dep.startswith(pattern[:-1]):
                    tags.update(t.strip() for t in mapped.split(","))
            elif dep == pattern:
                tags.update(t.strip() for t in mapped.split(","))
    return tags


def _services_from_python(repo_dir: Path) -> set[str]:
    tags: set[str] = set()
    blobs: list[str] = []
    for fname in ("pyproject.toml", "requirements.txt"):
        raw = _read_toml_raw(repo_dir / fname)
        if raw:
            blobs.append(raw.lower())
    if not blobs:
        return tags
    haystack = "\n".join(blobs)
    for dist, mapped in _PY_SERVICE_MAP.items():
        if dist in haystack:
            tags.update(t.strip() for t in mapped.split(","))
    return tags


def _services_from_terraform(repo_dir: Path) -> set[str]:
    tags: set[str] = set()
    # Scan root + one level of subdirectories, bounded to keep this cheap.
    tf_files: list[Path] = list(repo_dir.glob("*.tf"))
    for sub in sorted(repo_dir.iterdir()) if repo_dir.is_dir() else []:
        if sub.is_dir() and not sub.name.startswith("."):
            tf_files.extend(sub.glob("*.tf"))
        if len(tf_files) > 200:
            break
    for tf in tf_files[:200]:
        raw = _read_toml_raw(tf)  # plain text read; reused helper
        if not raw:
            continue
        for m in re.finditer(r'resource\s+"(aws_[a-z0-9_]+)"', raw):
            resource = m.group(1)
            for prefix, mapped in _TF_RESOURCE_MAP.items():
                if resource.startswith(prefix):
                    tags.add(mapped)
                    break
    return tags


def _services_from_compose(repo_dir: Path) -> set[str]:
    tags: set[str] = set()
    for fname in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        raw = _read_toml_raw(repo_dir / fname)
        if not raw:
            continue
        for m in re.finditer(r"^\s*image:\s*[\"']?([^\s\"']+)", raw, re.MULTILINE):
            image = m.group(1).lower()
            for needle, mapped in _COMPOSE_IMAGE_MAP.items():
                if needle in image:
                    tags.add(mapped)
    return tags


def _services_from_env(repo_dir: Path) -> set[str]:
    tags: set[str] = set()
    for fname in (".env.example", ".env.sample", ".env.template", ".env.dist"):
        raw = _read_toml_raw(repo_dir / fname)
        if not raw:
            continue
        upper = raw.upper()
        if "COGNITO_" in upper:
            tags.add("cognito")
        if "GOOGLE_CLIENT_ID" in upper or "GOOGLE_CLIENT_SECRET" in upper:
            tags.add("google-oauth")
        if "AUTH0_" in upper:
            tags.update(("auth0", "oauth"))
        if "REDIS_" in upper:
            tags.add("redis")
        for m in re.finditer(r"^DATABASE_URL\s*=\s*(.+)$", raw, re.MULTILINE | re.IGNORECASE):
            if "postgres" in m.group(1).lower():
                tags.add("postgres")
    return tags


def detect_services(repo_dir: Path) -> list[str]:
    """Detect cloud/auth/infra services from dependency and config files.

    Returns a deduplicated, sorted list of short technology tags (e.g.
    ["cognito", "oauth", "s3"]). Best-effort and dependency-free — each
    source is scanned independently and failures are swallowed.
    """
    tags: set[str] = set()
    tags |= _services_from_npm(repo_dir)
    tags |= _services_from_python(repo_dir)
    tags |= _services_from_terraform(repo_dir)
    tags |= _services_from_compose(repo_dir)
    tags |= _services_from_env(repo_dir)
    return sorted(tags)


def detect_project(
    repo_dir: Path,
    gitlab_project: VCSProject | None = None,
    gitlab_url: str = "",
) -> DetectedProject:
    """Run all detectors and return a complete DetectedProject."""
    primary_language, languages = detect_language(gitlab_project, repo_dir)
    roles = detect_roles(primary_language, languages, repo_dir)

    return DetectedProject(
        name=repo_dir.name,
        project_id=_get_project_id(gitlab_project, repo_dir),
        clone_url=detect_clone_url(repo_dir),
        gitlab_url=gitlab_url,
        default_branch=(
            gitlab_project.default_branch if gitlab_project else "main"
        ),
        primary_language=primary_language,
        languages=languages,
        roles=roles,
        framework=detect_framework(primary_language, repo_dir),
        ci_type=detect_ci_type(repo_dir),
        deploy_target=detect_deploy_target(repo_dir),
        test_command=detect_test_command(primary_language, repo_dir),
        lint_command=detect_lint_command(primary_language, repo_dir),
        archived=gitlab_project.archived if gitlab_project else False,
        services=detect_services(repo_dir),
    )


def _get_project_id(
    gitlab_project: VCSProject | None, repo_dir: Path
) -> str:
    """Get project path (owner/repo or group/repo) from VCS metadata or git remote."""
    if gitlab_project and gitlab_project.web_url:
        # Works for both GitLab and GitHub web URLs: https://host/owner/repo -> owner/repo
        match = re.match(r"https?://[^/]+/(.+)$", gitlab_project.web_url)
        if match:
            return match.group(1)

    # Fallback: parse from git remote (host-agnostic)
    try:
        import subprocess as _sp
        result = _sp.run(
            ["git", "-C", str(repo_dir), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        url = result.stdout.strip()
        m = re.match(r"git@[^:]+:(.+?)(?:\.git)?$", url) or \
            re.match(r"https?://[^/]+/(.+?)(?:\.git)?$", url)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""
