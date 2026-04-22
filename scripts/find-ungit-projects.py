"""Find directories that look like coding projects but have no .git directory."""

import argparse
import os
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
LOG_FILE = SCRIPT_DIR / "found-ungit-projects.log"

SKIP = {".git", ".worktrees", "node_modules", ".venv", "venv", "__pycache__", ".tox", "dist", "build"}

MARKER_FILES = {
    "package.json", "Cargo.toml", "pyproject.toml", "go.mod", "requirements.txt",
    "setup.py", "setup.cfg", "pom.xml", "build.gradle", "Makefile", "CMakeLists.txt",
    "Dockerfile", ".dockerignore", "docker-compose.yml", "docker-compose.yaml",
    "Taskfile.yml", "Taskfile.yaml", "Gemfile", "composer.json",
    "README.md", "README.rst", "README.txt", "LICENSE",
    ".editorconfig", ".prettierrc", ".eslintrc.json", "tsconfig.json",
}

MARKER_DIRS = {"src", ".idea", ".vscode", ".github"}


def is_project(dirpath: str, root: Path, dirnames: list[str], filenames: list[str]) -> bool:
    if Path(dirpath) == root:
        return False
    if ".git" in dirnames or ".git" in filenames:
        return False
    if MARKER_FILES & set(filenames):
        return True
    if MARKER_DIRS & set(dirnames):
        return True
    return False


def load_lines(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text().splitlines() if line.strip()}


def load_known() -> set[str]:
    if not LOG_FILE.exists():
        LOG_FILE.touch()
    known = load_lines(LOG_FILE)
    known |= load_lines(SCRIPT_DIR / "found-repos.log")
    return known


def is_worktree(path: str) -> bool:
    return ".worktrees" in Path(path).parts


def find_ungit_projects(root: Path) -> list[Path]:
    projects = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        if is_worktree(dirpath):
            dirnames.clear()
            continue
        if is_project(dirpath, root, dirnames, filenames):
            projects.append(Path(dirpath))
            dirnames.clear()
    return sorted(projects)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Find coding projects without .git")
    parser.add_argument("root", nargs="?", default="C:/projects", help="Root directory to search")
    args = parser.parse_args()

    known = load_known()
    projects = find_ungit_projects(Path(args.root))
    new_projects = [p for p in projects if str(p) not in known]

    if new_projects:
        with LOG_FILE.open("a") as f:
            for p in new_projects:
                f.write(f"{p}\n")
        print(f"Found {len(new_projects)} ungit projects (skipped {len(projects) - len(new_projects)} already known):\n")
        for p in new_projects:
            print(f"  {p}")
    else:
        print(f"No new ungit projects found ({len(known)} already known).")
