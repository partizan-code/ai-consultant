"""Bounded local UTF-8 knowledge snapshots and deterministic text search."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path

from ai_consultant_bot.config import Settings


@dataclass(frozen=True)
class KnowledgeChunk:
    source: str
    text: str


def search_terms(text: str) -> set[str]:
    # Short prefixes cover common Russian inflections without a morphology dependency.
    return {word[:5] for word in re.findall(r"[^\W_]+", text.casefold()) if len(word) >= 3}


def file_sections(path: Path, text: str) -> list[str]:
    if path.suffix.casefold() != ".csv":
        return [text]
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    headers = next(reader, [])
    return [
        "; ".join(
            f"{headers[index] if index < len(headers) else index + 1}: {value}"
            for index, value in enumerate(row)
            if value.strip()
        )
        for row in reader
    ]


class KnowledgeBase:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.chunks: tuple[KnowledgeChunk, ...] = ()

    def reload(self) -> int:
        """Publish a complete snapshot; missing directory means an empty business KB.

        Unreadable, oversized or invalid files abort reload, preserving the old snapshot.
        Symbolic links are excluded so the configured directory bounds file access.
        """
        root = self._settings.knowledge_base_path
        chunks: list[KnowledgeChunk] = []
        if root.is_symlink():
            raise ValueError("Knowledge directory must not be a symbolic link")
        if root.exists():
            self._read_directory(root, root, chunks)
        self.chunks = tuple(chunks)
        return len(chunks)

    def _read_directory(self, root: Path, directory: Path, chunks: list[KnowledgeChunk]) -> None:
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or path.name.startswith("."):
                continue
            if path.is_dir():
                self._read_directory(root, path, chunks)
                continue
            if not path.is_file() or path.suffix.casefold() not in {".md", ".txt", ".csv"}:
                continue
            with path.open("rb") as stream:
                raw = stream.read(self._settings.knowledge_file_max_bytes + 1)
            if len(raw) > self._settings.knowledge_file_max_bytes:
                raise ValueError("Knowledge file size limit exceeded")
            for section in file_sections(path, raw.decode("utf-8-sig")):
                size = self._settings.knowledge_chunk_chars
                for offset in range(0, len(section), size):
                    text = section[offset : offset + size].strip()
                    if text:
                        chunks.append(KnowledgeChunk(path.relative_to(root).as_posix(), text))
                        if len(chunks) > self._settings.knowledge_max_chunks:
                            raise ValueError("Knowledge chunk limit exceeded")

    def search(self, query: str) -> list[KnowledgeChunk]:
        terms = search_terms(query)
        ranked = [
            (len(terms & search_terms(chunk.text)), index, chunk)
            for index, chunk in enumerate(self.chunks)
        ]
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [chunk for score, _, chunk in ranked if score][
            : self._settings.knowledge_search_results
        ]
