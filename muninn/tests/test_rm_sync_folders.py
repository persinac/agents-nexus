import json
from pathlib import Path

import pytest

from muninn import rm_sync


def _write_meta(staging: Path, uuid: str, *, name: str, parent: str, type_: str = "DocumentType"):
    (staging / f"{uuid}.metadata").write_text(
        json.dumps({"visibleName": name, "parent": parent, "type": type_})
    )


@pytest.fixture
def staging(tmp_path: Path) -> Path:
    # Folder tree:
    #   Work/ (work-folder)
    #     Garner/ (garner-folder)
    #       Chatbot/ (chatbot-folder)
    #         notebook-1
    #   Personal/ (personal-folder)
    #     notebook-2
    #   notebook-3 (at root)
    #   notebook-4 (in trash)
    #   trashed-folder/ (in trash)
    #     notebook-5
    _write_meta(staging := tmp_path, "work-folder", name="Work", parent="", type_="CollectionType")
    _write_meta(staging, "garner-folder", name="Garner", parent="work-folder", type_="CollectionType")
    _write_meta(staging, "chatbot-folder", name="Chatbot", parent="garner-folder", type_="CollectionType")
    _write_meta(staging, "personal-folder", name="Personal", parent="", type_="CollectionType")
    _write_meta(staging, "trashed-folder", name="OldStuff", parent="trash", type_="CollectionType")

    _write_meta(staging, "notebook-1", name="Bot Notes", parent="chatbot-folder")
    _write_meta(staging, "notebook-2", name="Diary", parent="personal-folder")
    _write_meta(staging, "notebook-3", name="Inbox", parent="")
    _write_meta(staging, "notebook-4", name="Old Idea", parent="trash")
    _write_meta(staging, "notebook-5", name="Archived", parent="trashed-folder")
    return staging


def test_build_folder_map_resolves_nested_paths(staging: Path):
    m = rm_sync.build_folder_map(staging)
    assert m["work-folder"] == "Work"
    assert m["garner-folder"] == "Work/Garner"
    assert m["chatbot-folder"] == "Work/Garner/Chatbot"
    assert m["personal-folder"] == "Personal"


def test_build_folder_map_marks_trashed_folders(staging: Path):
    m = rm_sync.build_folder_map(staging)
    assert m["trashed-folder"] == "trash/OldStuff"


def test_notebook_folder_returns_resolved_path(staging: Path):
    m = rm_sync.build_folder_map(staging)
    assert rm_sync.notebook_folder(staging, "notebook-1", m) == "Work/Garner/Chatbot"
    assert rm_sync.notebook_folder(staging, "notebook-2", m) == "Personal"
    assert rm_sync.notebook_folder(staging, "notebook-3", m) == ""
    assert rm_sync.notebook_folder(staging, "notebook-4", m) == "trash"
    assert rm_sync.notebook_folder(staging, "notebook-5", m) == "trash/OldStuff"


def test_notebook_folder_builds_map_lazily(staging: Path):
    # Calling without an explicit map should still resolve correctly.
    assert rm_sync.notebook_folder(staging, "notebook-1") == "Work/Garner/Chatbot"
