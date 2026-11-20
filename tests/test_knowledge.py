"""Local knowledge ingestion/search; synthetic files under pytest's temporary directory."""

from dataclasses import replace
from pathlib import Path

import pytest

from ai_consultant_bot.config import Settings
from ai_consultant_bot.knowledge import KnowledgeBase


def test_formats_search_and_snapshot_reload(tmp_path: Path) -> None:
    (tmp_path / "sofas.md").write_text("# Диваны\nДиван Альфа: обивка велюр.", encoding="utf-8")
    (tmp_path / "delivery.txt").write_text("Доставка в Минск по согласованию.", encoding="utf-8")
    (tmp_path / "chairs.CSV").write_text(
        '\ufeffМодель,Описание\nБета,"Кресло, ткань рогожка"\n',
        encoding="utf-8",
    )
    (tmp_path / "ignored.json").write_text("private-ignored", encoding="utf-8")
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "ignored.txt").write_text("private-hidden", encoding="utf-8")
    knowledge = KnowledgeBase(Settings(knowledge_base_path=tmp_path))
    assert knowledge.reload() == 3
    assert knowledge.search("диваны")[0].source == "sofas.md"
    assert "Описание: Кресло, ткань рогожка" in knowledge.search("кресло")[0].text
    assert knowledge.search("несуществующее") == []
    assert knowledge.search("private") == []
    (tmp_path / "sofas.md").unlink()
    assert knowledge.search("диван")
    assert knowledge.reload() == 2
    assert not knowledge.search("диван")


def test_missing_directory_and_bounded_chunks(tmp_path: Path) -> None:
    settings = Settings(knowledge_base_path=tmp_path / "absent", knowledge_chunk_chars=15)
    knowledge = KnowledgeBase(settings)
    assert knowledge.reload() == 0
    settings.knowledge_base_path.mkdir()
    (settings.knowledge_base_path / "sample.txt").write_text("диван " * 20, encoding="utf-8")
    assert knowledge.reload() == 8
    assert all(len(chunk.text) <= 15 for chunk in knowledge.chunks)
    assert len(knowledge.search("диван")) == settings.knowledge_search_results


@pytest.mark.parametrize("failure", ["encoding", "size", "chunks"])
def test_failed_reload_preserves_snapshot(tmp_path: Path, failure: str) -> None:
    settings = Settings(knowledge_base_path=tmp_path)
    (tmp_path / "valid.txt").write_text("диван", encoding="utf-8")
    if failure == "size":
        settings = replace(settings, knowledge_file_max_bytes=20)
    if failure == "chunks":
        settings = replace(settings, knowledge_max_chunks=1)
    knowledge = KnowledgeBase(settings)
    knowledge.reload()
    snapshot = knowledge.chunks
    (tmp_path / "invalid.txt").write_bytes(b"\xff" if failure == "encoding" else b"x" * 50)
    with pytest.raises((ValueError, UnicodeError)):
        knowledge.reload()
    assert knowledge.chunks == snapshot


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


def test_semicolon_csv_keeps_headers_with_values(tmp_path: Path) -> None:
    (tmp_path / "sofas.csv").write_text("Модель;Ткань\nДиван Альфа;Велюр\n", encoding="utf-8")
    knowledge = KnowledgeBase(Settings(knowledge_base_path=tmp_path))
    assert knowledge.reload() == 1
    assert knowledge.search("диваны")[0].text == "Модель: Диван Альфа; Ткань: Велюр"
