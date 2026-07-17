"""Use Claude Haiku to auto-tag repos based on their file listings."""

import argparse
import json
import os
import sys
from pathlib import Path

import anthropic
import yaml

SCRIPT_DIR = Path(__file__).parent
MANIFEST_FILE = SCRIPT_DIR.parent / "repos-manifest.yaml"
BATCH_SIZE = 10

KNOWN_TAGS = [
    # Languages
    "python", "typescript", "javascript", "rust", "go", "java", "kotlin",
    "c", "cpp", "csharp", "ruby", "php", "elixir", "swift", "lua", "shell",
    # Frontend
    "frontend", "react", "vue", "angular", "svelte", "nextjs", "astro",
    "html", "css", "tailwind",
    # Backend
    "backend", "api", "rest", "graphql", "grpc", "websocket", "fastapi",
    "express", "django", "flask", "spring",
    # Data
    "database", "postgres", "mysql", "sqlite", "redis", "mongodb",
    "data-engineering", "etl", "streaming", "kafka", "trino", "spark",
    "analytics", "data-science", "ml", "llm",
    # Infra / DevOps
    "infrastructure", "docker", "kubernetes", "terraform", "pulumi",
    "ci-cd", "github-actions", "monitoring", "logging", "networking",
    "serverless", "cloud", "aws", "gcp", "azure",
    # Security
    "security", "ctf", "reverse-engineering", "firmware", "exploitation",
    "cryptography", "forensics",
    # IoT / Embedded
    "iot", "embedded", "esp32", "arduino", "raspberry-pi", "mqtt", "bluetooth",
    # AI / Agents
    "ai", "agentic", "orchestration", "chatbot", "nlp", "computer-vision",
    # Misc
    "cli", "library", "framework", "testing", "documentation",
    "game", "audio", "video", "media",
]

PROMPT_TEMPLATE = """You are a code repo tagger. Given repo names and either their file/directory listings
or metadata (url + existing tags), return a JSON object mapping each repo name to an array of tags.

When you have a file listing, use it to detect tech stack precisely.
When you only have a URL and existing tags, infer what you can from the repo name, GitHub org, and tags.

Preferred tags (use these when they fit):
{known_tags}

You may suggest 1-2 new tags if nothing above fits, but prefer the known list.
Return 3-8 tags per repo. Be specific over generic — "fastapi" over just "python" if you see FastAPI.
Return ONLY a JSON object. No explanation, no markdown fences.

{repos}"""


def get_file_listing(repo_path: Path, max_depth: int = 2) -> str:
    if not repo_path.is_dir():
        return ""

    lines = []
    root = repo_path

    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        if depth >= max_depth:
            dirnames.clear()
            continue

        dirnames[:] = [
            d for d in dirnames
            if d not in {".git", "node_modules", ".venv", "venv", "__pycache__",
                         ".tox", "dist", "build", ".mypy_cache", ".ruff_cache",
                         ".next", ".nuxt", "target", ".worktrees"}
        ]

        indent = "  " * depth

        for d in sorted(dirnames):
            lines.append(f"{indent}{d}/")
        for f in sorted(filenames):
            lines.append(f"{indent}{f}")

    return "\n".join(lines[:200])


def find_local_repos(scan_dirs: list[Path]) -> dict[str, Path]:
    repos: dict[str, Path] = {}
    for scan_dir in scan_dirs:
        if not scan_dir.is_dir():
            continue
        for child in scan_dir.iterdir():
            if child.is_dir():
                repos.setdefault(child.name, child)
        for child in scan_dir.iterdir():
            if child.is_dir():
                for grandchild in child.iterdir():
                    if grandchild.is_dir():
                        repos.setdefault(grandchild.name, grandchild)
    return repos


def load_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def tag_batch(client: anthropic.Anthropic, repos: list[tuple[str, str]]) -> dict[str, list[str]]:
    repo_sections = ""
    for name, listing in repos:
        repo_sections += f"### {name}\n```\n{listing}\n```\n\n"

    prompt = PROMPT_TEMPLATE.format(
        known_tags=json.dumps(KNOWN_TAGS),
        repos=repo_sections,
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]

    return json.loads(text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI-tag repos via Claude Haiku")
    parser.add_argument("--repos-dir", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Show what would be sent without calling Claude")
    parser.add_argument("--force", action="store_true", help="Re-tag repos that already have ai-tags")
    args = parser.parse_args()

    if not MANIFEST_FILE.exists():
        print(f"No {MANIFEST_FILE} — run build-manifest.py first.")
        sys.exit(1)

    api_key = load_api_key()
    if not api_key:
        print("No ANTHROPIC_API_KEY found in environment or .env file.")
        sys.exit(1)

    manifest = yaml.safe_load(MANIFEST_FILE.read_text()) or []
    manifest_by_name = {e["name"]: e for e in manifest}

    scan_dirs: list[Path] = []
    if args.repos_dir:
        scan_dirs = [args.repos_dir]
    else:
        for candidate in [Path("C:/projects"), Path.home() / "repos", Path.home() / "projects"]:
            if candidate.is_dir():
                scan_dirs.append(candidate)

    local_repos = find_local_repos(scan_dirs)

    to_tag: list[tuple[str, str]] = []
    for entry in manifest:
        name = entry["name"]
        if not args.force and "ai_tags" in entry:
            continue
        if name in local_repos:
            listing = get_file_listing(local_repos[name])
            if listing:
                to_tag.append((name, listing))
                continue
        # no local clone — build a minimal context from manifest metadata
        url = entry.get("url", "")
        tags = entry.get("tags", [])
        stub = f"url: {url}\nexisting_tags: {', '.join(tags)}"
        to_tag.append((name, stub))

    if not to_tag:
        print("All repos already tagged (use --force to re-tag).")
        sys.exit(0)

    print(f"Tagging {len(to_tag)} repos in batches of {BATCH_SIZE}...\n")

    if args.dry_run:
        for name, listing in to_tag:
            print(f"  {name} ({len(listing.splitlines())} lines)")
        sys.exit(0)

    client = anthropic.Anthropic(api_key=api_key)
    total_tagged = 0

    for i in range(0, len(to_tag), BATCH_SIZE):
        batch = to_tag[i:i + BATCH_SIZE]
        batch_names = [name for name, _ in batch]
        print(f"Batch {i // BATCH_SIZE + 1}: {', '.join(batch_names)}")

        try:
            results = tag_batch(client, batch)
        except (json.JSONDecodeError, anthropic.APIError) as e:
            print(f"  ERROR: {e}")
            continue

        for name, tags in results.items():
            if name in manifest_by_name:
                manifest_by_name[name]["ai_tags"] = sorted(set(tags))
                total_tagged += 1
                print(f"  {name}: {tags}")

        print()

    entries = sorted(manifest_by_name.values(), key=lambda e: e["name"].lower())
    MANIFEST_FILE.write_text(yaml.dump(entries, default_flow_style=False, sort_keys=False))

    print(f"Done. Tagged {total_tagged} repos. Manifest updated.")
