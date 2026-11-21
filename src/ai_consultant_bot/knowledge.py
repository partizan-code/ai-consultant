"""Bounded, atomic snapshots of complete local UTF-8 knowledge documents."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ai_consultant_bot.config import Settings


@dataclass(frozen=True)
class KnowledgeDocument:
    source: str
    text: str


class KnowledgeLimitError(ValueError):
    """A safe, user-facing limit error without file contents or local paths."""


class KnowledgeBase:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.documents: tuple[KnowledgeDocument, ...] = ()

    def reload(self) -> int:
        """Publish a complete snapshot; missing directory means an empty business KB.

        Unreadable, oversized or invalid files abort reload, preserving the old snapshot.
        Symbolic links are excluded so the configured directory bounds file access.
        """
        root = self._settings.knowledge_base_path
        documents: list[KnowledgeDocument] = []
        characters = 0
        if root.is_symlink():
            raise ValueError("Knowledge directory must not be a symbolic link")
        if root.exists():
            for document in self._read_directory(root, root):
                characters += len(document.text)
                if characters > self._settings.knowledge_max_chars:
                    raise KnowledgeLimitError(
                        f"База знаний превышает лимит {self._settings.knowledge_max_chars} символов."
                    )
                documents.append(document)
                if len(documents) > self._settings.knowledge_max_documents:
                    raise KnowledgeLimitError(
                        "Превышен лимит документов базы знаний: "
                        f"{self._settings.knowledge_max_documents}."
                    )
        self.documents = tuple(documents)
        return len(documents)

    def _read_directory(self, root: Path, directory: Path) -> Iterator[KnowledgeDocument]:
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or path.name.startswith("."):
                continue
            if path.is_dir():
                yield from self._read_directory(root, path)
                continue
            if not path.is_file() or path.suffix.casefold() not in {".md", ".txt", ".csv"}:
                continue
            with path.open("rb") as stream:
                raw = stream.read(self._settings.knowledge_file_max_bytes + 1)
            if len(raw) > self._settings.knowledge_file_max_bytes:
                raise KnowledgeLimitError(
                    "Файл базы знаний превышает лимит "
                    f"{self._settings.knowledge_file_max_bytes} байт."
                )
            text = raw.decode("utf-8-sig")
            if text.strip():
                yield KnowledgeDocument(path.relative_to(root).as_posix(), text)
