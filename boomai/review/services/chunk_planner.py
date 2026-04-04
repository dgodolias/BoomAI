from __future__ import annotations

from ...core.config import settings
from ..runtime_policy import effective_scan_char_budget


class ChunkPlanner:
    """Owns chunk sizing and single-file section splitting decisions."""

    def chunk_files(
        self,
        file_contents: list[tuple[str, str]],
        char_budget: int,
    ) -> list[list[tuple[str, str]]]:
        """Split files into chunks that fit within the effective character budget."""
        effective_budget = effective_scan_char_budget(char_budget)
        chunks: list[list[tuple[str, str]]] = []
        current_chunk: list[tuple[str, str]] = []
        current_size = 0

        sorted_files = sorted(file_contents, key=lambda item: len(item[1]), reverse=True)

        for path, content in sorted_files:
            file_size = len(content) + len(path) + 20
            needs_split = current_chunk and (
                current_size + file_size > effective_budget
                or len(current_chunk) >= settings.scan_max_files_per_chunk
            )
            if needs_split:
                chunks.append(current_chunk)
                current_chunk = []
                current_size = 0
            current_chunk.append((path, content))
            current_size += file_size

        if current_chunk:
            chunks.append(current_chunk)

        return chunks if chunks else [[]]

    def split_single_file_into_sections(
        self,
        path: str,
        content: str,
        *,
        target_chars: int = 28_000,
        overlap_lines: int = 30,
    ) -> list[tuple[str, list[tuple[str, str]]]]:
        """Split one large file into overlapping numbered sections."""
        lines = content.replace("\r\n", "\n").split("\n")
        if not lines:
            return [(f"{path}:1-1", [(path, content)])]

        sections: list[tuple[str, list[tuple[str, str]]]] = []
        start = 0
        total = len(lines)

        while start < total:
            end = start
            chars = 0
            while end < total:
                candidate = lines[end]
                line_cost = len(candidate) + 12
                if end > start and chars + line_cost > target_chars:
                    break
                chars += line_cost
                end += 1

            if end <= start:
                end = min(total, start + 1)

            label = f"{path}:{start + 1}-{end}"
            section_text = self.build_numbered_section_content(lines, start, end)
            sections.append((label, [(path, section_text)]))

            if end >= total:
                break
            start = max(start + 1, end - overlap_lines)

        return sections

    @staticmethod
    def build_numbered_section_content(lines: list[str], start: int, end: int) -> str:
        """Build a numbered line window so line references stay meaningful."""
        numbered = [
            f"{line_no:5d}: {line}"
            for line_no, line in enumerate(lines[start:end], start=start + 1)
        ]
        return "\n".join(numbered)
