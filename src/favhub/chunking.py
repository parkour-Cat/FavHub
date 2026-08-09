"""Deterministic Markdown block chunking for local retrieval."""

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContentChunk:
    """A traceable piece of Markdown content."""

    ordinal: int
    relative_path: str
    line_start: int
    line_end: int
    heading: str | None
    text: str


@dataclass(frozen=True, slots=True)
class _LogicalBlock:
    text: str
    line_start: int
    line_end: int
    heading: str | None


_HEADING = re.compile(r" {0,3}(#{1,6})(?:[ \t]+(.*?)|[ \t]*)$")


def _heading_text(line: str) -> str | None:
    match = _HEADING.fullmatch(line)
    if match is None:
        return None
    value = (match.group(2) or "").strip()
    value = re.sub(r"[ \t]+#+[ \t]*$", "", value).strip()
    return value


def _fence_start(line: str) -> tuple[str, int] | None:
    indent = len(line) - len(line.lstrip(" "))
    if indent > 3:
        return None
    candidate = line[indent:]
    if len(candidate) < 3 or candidate[0] not in "`~":
        return None
    marker = candidate[0]
    length = len(candidate) - len(candidate.lstrip(marker))
    if length < 3:
        return None
    return marker, length


def _fence_end(line: str, marker: str, length: int) -> bool:
    indent = len(line) - len(line.lstrip(" "))
    if indent > 3:
        return False
    candidate = line[indent:]
    if not candidate.startswith(marker * length):
        return False
    return not candidate[len(marker * length) :].strip(marker).strip()


def _make_block(lines: list[str], start: int, heading: str | None) -> _LogicalBlock:
    return _LogicalBlock(
        text="\n".join(lines),
        line_start=start + 1,
        line_end=start + len(lines),
        heading=heading,
    )


def _parse_blocks(text: str) -> tuple[_LogicalBlock, ...]:
    lines = text.splitlines()
    blocks: list[_LogicalBlock] = []
    current_heading: str | None = None
    index = 0

    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue

        heading = _heading_text(lines[index])
        if heading is not None:
            current_heading = heading
            heading_start = index
            index += 1
            while index < len(lines) and not lines[index].strip():
                index += 1
            if index == len(lines) or _heading_text(lines[index]) is not None:
                blocks.append(
                    _make_block(lines[heading_start : heading_start + 1], heading_start, heading)
                )
                continue

            if _fence_start(lines[index]) is not None:
                marker, marker_length = _fence_start(lines[index]) or ("`", 3)
                end = index + 1
                while end < len(lines) and not _fence_end(lines[end], marker, marker_length):
                    end += 1
                if end < len(lines):
                    end += 1
                blocks.append(_make_block(lines[heading_start:end], heading_start, heading))
                index = end
                continue

            end = index + 1
            while end < len(lines):
                if not lines[end].strip() or _heading_text(lines[end]) is not None:
                    break
                if _fence_start(lines[end]) is not None:
                    break
                end += 1
            blocks.append(_make_block(lines[heading_start:end], heading_start, heading))
            index = end
            continue

        if _fence_start(lines[index]) is not None:
            marker, marker_length = _fence_start(lines[index]) or ("`", 3)
            end = index + 1
            while end < len(lines) and not _fence_end(lines[end], marker, marker_length):
                end += 1
            if end < len(lines):
                end += 1
            blocks.append(_make_block(lines[index:end], index, current_heading))
            index = end
            continue

        start = index
        end = index + 1
        while end < len(lines):
            if not lines[end].strip() or _heading_text(lines[end]) is not None:
                break
            if _fence_start(lines[end]) is not None:
                break
            end += 1
        blocks.append(_make_block(lines[start:end], start, current_heading))
        index = end

    return tuple(blocks)


def _has_fence(block: _LogicalBlock) -> bool:
    return any(_fence_start(line) is not None for line in block.text.splitlines())


def _merge_small_blocks(
    blocks: tuple[_LogicalBlock, ...], lines: list[str], max_chars: int
) -> tuple[_LogicalBlock, ...]:
    """Pack consecutive blocks under one heading up to the chunk size.

    A blank line ends a block, which is the right reading of Markdown and the
    wrong unit to retrieve. Plenty of writing puts every sentence in its own
    paragraph — Zhihu answers overwhelmingly do — and without this the index
    fills with chunks a dozen characters long: too small to mean anything on
    their own, numerous enough to crowd other items out of the candidate pool,
    and each one still costing a vector.

    Merged text is rebuilt from the original line slice rather than joined with
    a separator, so the blank lines between blocks stay in place and a chunk's
    reported line range keeps pointing at the lines it actually came from.
    """
    merged: list[_LogicalBlock] = []
    for block in blocks:
        previous = merged[-1] if merged else None
        if (
            previous is not None
            and previous.heading == block.heading
            and not _has_fence(previous)
            and not _has_fence(block)
        ):
            combined = "\n".join(lines[previous.line_start - 1 : block.line_end])
            if len(combined) <= max_chars:
                merged[-1] = _LogicalBlock(
                    text=combined,
                    line_start=previous.line_start,
                    line_end=block.line_end,
                    heading=previous.heading,
                )
                continue
        merged.append(block)
    return tuple(merged)


def _line_range(block: _LogicalBlock, start: int, end: int) -> tuple[int, int]:
    line_start = block.line_start + block.text.count("\n", 0, start)
    line_end = block.line_start + block.text.count("\n", 0, end - 1)
    return line_start, line_end


def chunk_markdown(
    relative_path: str, text: str, max_chars: int = 1200, overlap: int = 120
) -> tuple[ContentChunk, ...]:
    """Split Markdown into deterministic, line-traceable content chunks."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if overlap < 0:
        raise ValueError("overlap must be non-negative")
    if overlap >= max_chars:
        raise ValueError("overlap must be less than max_chars")

    chunks: list[ContentChunk] = []
    lines = text.splitlines()
    for block in _merge_small_blocks(_parse_blocks(text), lines, max_chars):
        position = 0
        while position < len(block.text):
            end = min(position + max_chars, len(block.text))
            piece = block.text[position:end]
            if piece.strip():
                line_start, line_end = _line_range(block, position, end)
                chunks.append(
                    ContentChunk(
                        ordinal=len(chunks),
                        relative_path=relative_path,
                        line_start=line_start,
                        line_end=line_end,
                        heading=block.heading,
                        text=piece,
                    )
                )
            if end == len(block.text):
                break
            position = end - overlap
    return tuple(chunks)
