"""Complete local snapshots; synthetic files under pytest's temporary directory."""

from dataclasses import replace
from pathlib import Path

import pytest

from ai_consultant_bot.config import Settings
from ai_consultant_bot.knowledge import KnowledgeBase, KnowledgeLimitError


def test_complete_files_stable_order_and_snapshot_reload(tmp_path: Path) -> None:
    texts = {
        "sofas.md": "# Диваны\n" + "Диван Альфа: обивка велюр.\n" * 100,
        "delivery.txt": "Доставка в Минск по согласованию.\n",
        "catalog/chairs.CSV": 'Модель,Описание\nБета,"Кресло, ткань рогожка"\n',
    }
    (tmp_path / "catalog").mkdir()
    for name, text in texts.items():
        (tmp_path / name).write_bytes(("\ufeff" + text).encode("utf-8"))
    (tmp_path / "ignored.json").write_text("private-ignored", encoding="utf-8")
    (tmp_path / "empty.md").write_text(" \n", encoding="utf-8")
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "ignored.txt").write_text("private-hidden", encoding="utf-8")
    knowledge = KnowledgeBase(Settings(knowledge_base_path=tmp_path))
    assert knowledge.reload() == 3
    assert [document.source for document in knowledge.documents] == sorted(texts)
    assert {document.source: document.text for document in knowledge.documents} == texts
    snapshot = knowledge.documents
    assert knowledge.reload() == 3
    assert knowledge.documents == snapshot
    (tmp_path / "sofas.md").unlink()
    assert knowledge.documents == snapshot
    assert knowledge.reload() == 2
    assert all(document.source != "sofas.md" for document in knowledge.documents)


def test_missing_directory_and_exact_character_limit(tmp_path: Path) -> None:
    settings = Settings(knowledge_base_path=tmp_path / "absent", knowledge_max_chars=10)
    knowledge = KnowledgeBase(settings)
    assert knowledge.reload() == 0
    settings.knowledge_base_path.mkdir()
    (settings.knowledge_base_path / "sample.txt").write_text("диван" * 2, encoding="utf-8")
    assert knowledge.reload() == 1
    assert knowledge.documents[0].text == "диван" * 2
    (settings.knowledge_base_path / "another.md").write_text("я", encoding="utf-8")
    snapshot = knowledge.documents
    with pytest.raises(KnowledgeLimitError, match="10 символов"):
        knowledge.reload()
    assert knowledge.documents is snapshot


@pytest.mark.parametrize("failure", ["encoding", "size", "documents", "characters"])
def test_failed_reload_preserves_snapshot(tmp_path: Path, failure: str) -> None:
    settings = Settings(knowledge_base_path=tmp_path)
    (tmp_path / "valid.txt").write_text("диван", encoding="utf-8")
    if failure == "size":
        settings = replace(settings, knowledge_file_max_bytes=20)
    if failure == "documents":
        settings = replace(settings, knowledge_max_documents=1)
    if failure == "characters":
        settings = replace(settings, knowledge_max_chars=30)
    knowledge = KnowledgeBase(settings)
    knowledge.reload()
    snapshot = knowledge.documents
    (tmp_path / "invalid.txt").write_bytes(b"\xff" if failure == "encoding" else b"x" * 50)
    with pytest.raises((ValueError, UnicodeError)):
        knowledge.reload()
    assert knowledge.documents is snapshot


def test_symbolic_links_are_not_read(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("секрет", encoding="utf-8")
    try:
        (root / "linked.txt").symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks are unavailable")
    knowledge = KnowledgeBase(Settings(knowledge_base_path=root))
    assert knowledge.reload() == 0


def test_semicolon_csv_is_preserved_in_full(tmp_path: Path) -> None:
    text = "Модель;Ткань\nДиван Альфа;Велюр\n"
    (tmp_path / "sofas.csv").write_bytes(text.encode("utf-8"))
    knowledge = KnowledgeBase(Settings(knowledge_base_path=tmp_path))
    assert knowledge.reload() == 1
    assert knowledge.documents[0].text == text
