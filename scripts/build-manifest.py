"""Build repos-manifest.yaml from clone-urls.txt + local repo scanning."""

import argparse
import os
import re
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).parent
MANIFEST_FILE = SCRIPT_DIR.parent / "repos-manifest.yaml"
CLONE_URLS = SCRIPT_DIR / "clone-urls.txt"

# ── Personal category rules (matched against repo name + url) ────────────────

CATEGORY_RULES = [
    # Personal projects
    (r"lockfale|cackalacky|ckc-", ["cackalackycon", "hacking", "iot"]),
    (r"flippin.?balls|flashback.?fleet", ["pinball-biz"]),
    (r"espressif|esp32|esp8266", ["iot"]),
    (r"bungie|bungo|\bd2\b|d2utility|destiny|game.?damage", ["destiny-2"]),
    (r"\bjcp\b|barbell", ["jcp-barbell-club"]),
    (r"homelab|nebula|talos|k8s.?setup", ["homelab", "infra"]),

    # Agent / AI
    (r"agents.?nexus|claude.?agents", ["infra", "agentic", "orchestration"]),
    (r"langgraph|autogen|swarm", ["agentic", "orchestration", "reference"]),
    (r"anthropic|openai", ["ai", "reference"]),
    (r"oracle|oracle.?py|oracle.?web", ["ai", "personal-project"]),
    (r"discordbot|guidebot|stockdatabot|d2utility_bot", ["bot"]),

    # Reference repos
    (r"prefect|dagster|arrow|duckdb|sqlglot", ["data-engineering", "reference"]),
    (r"ray|temporal|consul|vitess", ["distributed", "reference"]),
    (r"ruff|uv|pydantic|fastapi", ["engineering-practices", "reference"]),
    (r"vercel.*chat|ai.?chat", ["ai", "chat", "reference"]),
    (r"grafana.?dashboards|kube.?prometheus|prometheus.?operator|strimzi", ["monitoring", "kubernetes", "reference"]),

    # Data / finance
    (r"stockdata|cash.?flow|kwhco2e|training.?rebalance", ["data", "finance"]),
    (r"trino|hive", ["data-engineering"]),

    # Web apps
    (r"learningledger|quizzapper|llcwebsite|wrf", ["web-app"]),
    (r"react.?playground|ip.?react|space.?soccer|server.?soccer", ["web-app"]),
    (r"persinac\.github|mrastgoo\.github", ["website"]),

    # Infra / DevOps
    (r"amplify|sample.?app|getting.?started|templates|sampleexample", ["starter", "learning"]),
    (r"adventofcode|practice|practice.?problems|interview|til", ["learning"]),
    (r"gcghealthcheck|ip.?api|ipn.?middle|monitor.?select|management.?center", ["tooling"]),
    (r"highway.?to.?hell", ["personal-project"]),
    (r"scoop|pyenv", ["dev-tools"]),
    (r"nmig", ["database", "migration"]),
    (r"remarkable|imagelabeler", ["tooling"]),
    (r"opus.?media|opus.?recorder|raw.?opus|wrtc", ["media", "webrtc"]),
    (r"appian.?linter", ["tooling", "linter"]),
    (r"fantasynames|random", ["personal-project"]),
    (r"d3coordinate|overwolf", ["gaming"]),
]

# ── Tech stack detection (matched against files/dirs in repo root) ───────────

FILE_SIGNALS = {
    # Languages
    "pyproject.toml":       ["python"],
    "setup.py":             ["python"],
    "setup.cfg":            ["python"],
    "requirements.txt":     ["python"],
    "Pipfile":              ["python"],
    "package.json":         ["javascript"],
    "tsconfig.json":        ["typescript"],
    "Cargo.toml":           ["rust"],
    "go.mod":               ["go"],
    "pom.xml":              ["java"],
    "build.gradle":         ["java", "gradle"],
    "build.gradle.kts":     ["kotlin", "gradle"],
    "Gemfile":              ["ruby"],
    "composer.json":        ["php"],
    "mix.exs":              ["elixir"],
    "Package.swift":        ["swift"],
    "CMakeLists.txt":       ["c-cpp", "cmake"],
    "Makefile":             ["make"],
    "*.sln":                ["dotnet"],
    "*.csproj":             ["dotnet", "csharp"],
    "*.fsproj":             ["dotnet", "fsharp"],
    "dune-project":         ["ocaml"],
    "platformio.ini":       ["iot", "embedded"],

    # Frameworks
    "next.config.js":       ["nextjs", "react"],
    "next.config.ts":       ["nextjs", "react"],
    "next.config.mjs":      ["nextjs", "react"],
    "nuxt.config.ts":       ["nuxt", "vue"],
    "angular.json":         ["angular"],
    "svelte.config.js":     ["svelte"],
    "astro.config.mjs":     ["astro"],
    "tailwind.config.js":   ["tailwind"],
    "tailwind.config.ts":   ["tailwind"],
    "vite.config.ts":       ["vite"],
    "vite.config.js":       ["vite"],
    "webpack.config.js":    ["webpack"],

    # Infra / DevOps
    "Dockerfile":           ["docker"],
    "docker-compose.yml":   ["docker", "compose"],
    "docker-compose.yaml":  ["docker", "compose"],
    "Taskfile.yml":         ["taskfile"],
    "Taskfile.yaml":        ["taskfile"],
    "Makefile":             ["make"],
    "Justfile":             ["just"],
    "Earthfile":            ["earthly"],
    "Vagrantfile":          ["vagrant"],
    "Pulumi.yaml":          ["pulumi", "iac"],
    "serverless.yml":       ["serverless"],
    "fly.toml":             ["fly-io"],
    "render.yaml":          ["render"],
    "vercel.json":          ["vercel"],
    "netlify.toml":         ["netlify"],
    "dagger.json":          ["dagger"],

    # Testing
    "jest.config.js":       ["jest"],
    "jest.config.ts":       ["jest"],
    "vitest.config.ts":     ["vitest"],
    "pytest.ini":           ["pytest"],
    "tox.ini":              ["tox"],
    "cypress.config.js":    ["cypress"],
    "playwright.config.ts": ["playwright"],

    # Data
    "dbt_project.yml":      ["dbt", "data-engineering"],

    # AI/ML
    "model.py":             ["ml"],
    "train.py":             ["ml"],
}

DIR_SIGNALS = {
    "terraform":    ["terraform", "iac"],
    ".github":      ["github-actions"],
    ".circleci":    ["circleci"],
    "helm":         ["helm", "kubernetes"],
    "k8s":          ["kubernetes"],
    "proto":        ["protobuf", "grpc"],
    "migrations":   ["database"],
    "prisma":       ["prisma", "database"],
}


def classify_by_name(name: str, url: str) -> list[str]:
    text = f"{name} {url}".lower()
    tags = []
    for pattern, cats in CATEGORY_RULES:
        if re.search(pattern, text):
            tags.extend(cats)
    return tags


def detect_stack(repo_path: Path) -> list[str]:
    if not repo_path.is_dir():
        return []

    tags = []
    entries = set(os.listdir(repo_path))

    for pattern, file_tags in FILE_SIGNALS.items():
        if "*" in pattern:
            suffix = pattern.replace("*", "")
            if any(e.endswith(suffix) for e in entries):
                tags.extend(file_tags)
        elif pattern in entries:
            tags.extend(file_tags)

    for dirname, dir_tags in DIR_SIGNALS.items():
        if dirname in entries:
            tags.extend(dir_tags)

    return tags


def detect_owner(url: str) -> str:
    if "persinac" in url:
        return "personal"
    return "community"


def load_existing_manifest() -> dict[str, dict]:
    if not MANIFEST_FILE.exists():
        return {}
    entries = yaml.safe_load(MANIFEST_FILE.read_text()) or []
    return {e["name"]: e for e in entries}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build repos manifest")
    parser.add_argument("--repos-dir", type=Path, default=None,
                        help="Local directory containing cloned repos for stack detection")
    args = parser.parse_args()

    if not CLONE_URLS.exists():
        print(f"No {CLONE_URLS} found — run find-repos.py + extract-urls.py first.")
        raise SystemExit(1)

    existing = load_existing_manifest()
    urls = [line.strip() for line in CLONE_URLS.read_text().splitlines() if line.strip()]

    # collect all repo dirs for scanning
    scan_dirs: list[Path] = []
    if args.repos_dir:
        scan_dirs = [args.repos_dir]
    else:
        # auto-detect: check common locations
        for candidate in [Path("C:/projects"), Path.home() / "repos", Path.home() / "projects"]:
            if candidate.is_dir():
                scan_dirs.append(candidate)

    # build a name -> local path lookup
    local_repos: dict[str, Path] = {}
    for scan_dir in scan_dirs:
        if not scan_dir.is_dir():
            continue
        for child in scan_dir.iterdir():
            if child.is_dir():
                local_repos[child.name] = child
        # also check one level deeper (e.g. repos/reference/langgraph)
        for child in scan_dir.iterdir():
            if child.is_dir():
                for grandchild in child.iterdir():
                    if grandchild.is_dir():
                        local_repos.setdefault(grandchild.name, grandchild)

    for url in urls:
        name = url.rstrip("/").split("/")[-1].removesuffix(".git")

        category_tags = classify_by_name(name, url)
        stack_tags = detect_stack(local_repos.get(name, Path("__missing__")))
        all_tags = sorted(set(category_tags + stack_tags)) or ["uncategorized"]

        if name in existing:
            existing[name]["url"] = url
            # merge new auto-detected tags with existing, preserving manual edits
            old_tags = set(existing[name].get("tags", []))
            existing[name]["tags"] = sorted(old_tags | set(all_tags) - {"uncategorized"} if old_tags else set(all_tags))
            continue

        existing[name] = {
            "name": name,
            "url": url,
            "tags": all_tags,
            "owner": detect_owner(url),
        }

    entries = sorted(existing.values(), key=lambda e: e["name"].lower())
    MANIFEST_FILE.write_text(yaml.dump(entries, default_flow_style=False, sort_keys=False))

    # summary
    tag_counts: dict[str, int] = {}
    for e in entries:
        for t in e["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1

    print(f"Manifest: {len(entries)} repos -> {MANIFEST_FILE}\n")
    print("Tags:")
    for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1]):
        print(f"  {tag}: {count}")

    uncategorized = [e["name"] for e in entries if "uncategorized" in e["tags"]]
    if uncategorized:
        print(f"\nUncategorized ({len(uncategorized)}):")
        for name in uncategorized:
            print(f"  {name}")
