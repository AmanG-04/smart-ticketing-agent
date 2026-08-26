import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rank_bm25 import BM25Okapi

from src.config import KB_DIR

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_ERROR_CODE_RE = re.compile(r"\b[A-Z][A-Z0-9_]{4,}\b|\b\d{3} (?:Forbidden|Not Found|Unauthorized)\b")


@dataclass
class KBChunk:
    doc_title: str
    path: str
    heading_path: list[str] = field(default_factory=list)
    text: str = ""

    @property
    def location(self) -> str:
        return f"{self.doc_title} > {' > '.join(self.heading_path)}" if self.heading_path else self.doc_title


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _split_markdown(path: Path) -> list[KBChunk]:
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    doc_title = next((l[2:].strip() for l in lines if l.startswith("# ")), path.stem)

    chunks: list[KBChunk] = []
    current_heading: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        text = "\n".join(buffer).strip()
        if text:
            chunks.append(
                KBChunk(
                    doc_title=doc_title,
                    path=str(path.relative_to(path.parents[2])),
                    heading_path=list(current_heading),
                    text=text,
                )
            )
        buffer.clear()

    for line in lines:
        stripped = line.strip()
        if stripped == "---":
            flush()
            continue
        if stripped.startswith("## "):
            flush()
            current_heading = [stripped[3:].strip()]
            continue
        if stripped.startswith("### "):
            flush()
            current_heading = current_heading[:1] + [stripped[4:].strip()]
            continue
        buffer.append(line)
    flush()
    return chunks


def load_corpus(kb_dir: Optional[Path] = None) -> list[KBChunk]:
    kb_dir = kb_dir or KB_DIR
    corpus: list[KBChunk] = []
    for md in sorted(kb_dir.rglob("*.md")):
        corpus.extend(_split_markdown(md))
    return corpus


def extract_error_codes(text: str) -> list[str]:
    return sorted(set(_ERROR_CODE_RE.findall(text or "")))


class KnowledgeBase:
    def __init__(self, kb_dir: Optional[Path] = None) -> None:
        self.chunks = load_corpus(kb_dir)
        self._bm25 = BM25Okapi([_tokenize(c.text) for c in self.chunks])
        self._code_index: list[set[str]] = [
            set(extract_error_codes(c.text)) for c in self.chunks
        ]

    def search(self, query: str, k: int = 4) -> list[tuple[KBChunk, float]]:
        tokens = _tokenize(query)
        if not tokens:
            return []
        scores = self._bm25.get_scores(tokens)
        codes = set(extract_error_codes(query))
        boosted = []
        for idx, (chunk, score) in enumerate(zip(self.chunks, scores)):
            overlap = len(codes & self._code_index[idx]) if codes else 0
            boosted.append((chunk, float(score) + 2.0 * overlap))
        boosted.sort(key=lambda pair: pair[1], reverse=True)
        return [(c, s) for c, s in boosted[:k] if s > 0]

    def build_context(self, hits: list[tuple[KBChunk, float]], max_chars: int = 6000) -> str:
        parts: list[str] = []
        used = 0
        for chunk, _score in hits:
            block = f"[{chunk.location}]\n{chunk.text}"
            if used + len(block) > max_chars:
                break
            parts.append(block)
            used += len(block)
        return "\n\n---\n\n".join(parts)

    def render_context(self, query: str, k: int = 4, max_chars: int = 6000) -> str:
        return self.build_context(self.search(query, k=k), max_chars=max_chars)


_kb: Optional[KnowledgeBase] = None


def get_kb() -> KnowledgeBase:
    global _kb
    if _kb is None:
        _kb = KnowledgeBase()
    return _kb
