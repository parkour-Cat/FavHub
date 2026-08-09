from dataclasses import FrozenInstanceError

import pytest

from favhub.chunking import ContentChunk, chunk_markdown


def test_short_file_produces_one_immutable_chunk() -> None:
    chunks = chunk_markdown("content.md", "A short note.")

    assert chunks == (
        ContentChunk(
            ordinal=0,
            relative_path="content.md",
            line_start=1,
            line_end=1,
            heading=None,
            text="A short note.",
        ),
    )
    assert not hasattr(chunks[0], "__dict__")
    with pytest.raises(FrozenInstanceError):
        chunks[0].text = "changed"  # type: ignore[misc]


def test_headings_and_blank_lines_form_logical_blocks() -> None:
    text = "# Intro\n\nFirst paragraph.\n\nSecond paragraph.\n\n## Details\n\nMore."

    chunks = chunk_markdown("notes/guide.md", text)

    # Both paragraphs sit under Intro and fit together, so they retrieve
    # together; the heading is the boundary that survives.
    assert [(chunk.heading, chunk.text) for chunk in chunks] == [
        ("Intro", "# Intro\n\nFirst paragraph.\n\nSecond paragraph."),
        ("Details", "## Details\n\nMore."),
    ]
    assert [chunk.ordinal for chunk in chunks] == list(range(2))
    assert [chunk.relative_path for chunk in chunks] == ["notes/guide.md"] * 2
    assert [(chunk.line_start, chunk.line_end) for chunk in chunks] == [(1, 5), (7, 9)]


def test_sentence_per_paragraph_prose_does_not_become_a_chunk_per_sentence() -> None:
    """The shape that made 27,918 characters of Zhihu answer into 965 chunks.

    A chunk of fifteen characters cannot answer anything on its own, and a
    single item holding hundreds of them crowds every other item out of the
    candidate pool while paying for a vector apiece.
    """
    text = "\n\n".join(f"第{index}句话，很短。" for index in range(40))

    chunks = chunk_markdown("content.md", text)

    assert len(chunks) < 5
    assert min(len(chunk.text) for chunk in chunks) > 200
    # Every source line still lands in some chunk, in order.
    assert "".join(chunk.text for chunk in chunks).count("句话") == 40


def test_merged_chunks_still_point_at_the_lines_they_came_from() -> None:
    lines = ["alpha", "", "beta", "", "", "gamma"]
    text = "\n".join(lines)

    chunks = chunk_markdown("content.md", text)

    assert len(chunks) == 1
    assert (chunks[0].line_start, chunks[0].line_end) == (1, 6)
    # Reading those source lines back gives exactly the chunk's text, blank
    # lines included — a citation that drifts by a line is a broken citation.
    assert "\n".join(lines[0:6]) == chunks[0].text


def test_merging_stops_at_the_chunk_size() -> None:
    text = "\n\n".join(["x" * 60] * 10)

    chunks = chunk_markdown("content.md", text, max_chars=200)

    assert all(len(chunk.text) <= 200 for chunk in chunks)
    # Three 60-char paragraphs plus their blank lines fit; four do not.
    assert [len(chunk.text) for chunk in chunks] == [184, 184, 184, 60]


def test_merging_never_crosses_a_heading() -> None:
    text = "# One\n\nshort\n\n# Two\n\nalso short"

    chunks = chunk_markdown("content.md", text)

    assert [chunk.heading for chunk in chunks] == ["One", "Two"]


def test_merging_leaves_fenced_code_alone() -> None:
    text = "before\n\n```\ncode\n```\n\nafter"

    chunks = chunk_markdown("content.md", text)

    assert [chunk.text for chunk in chunks] == ["before", "```\ncode\n```", "after"]


def test_whitespace_only_regions_do_not_emit_chunks() -> None:
    chunks = chunk_markdown("content.md", "  \n\n\t\nParagraph.\n\n   ")

    assert [chunk.text for chunk in chunks] == ["Paragraph."]
    assert [(chunk.line_start, chunk.line_end) for chunk in chunks] == [(4, 4)]


def test_fenced_code_stays_together_and_markdown_inside_is_literal() -> None:
    text = "# Example\n\n```python\nvalue = 1\n\n# not a heading\n```\n\nAfter."

    chunks = chunk_markdown("content.md", text, max_chars=200)

    assert [(chunk.heading, chunk.text) for chunk in chunks] == [
        ("Example", "# Example\n\n```python\nvalue = 1\n\n# not a heading\n```"),
        ("Example", "After."),
    ]
    assert [(chunk.line_start, chunk.line_end) for chunk in chunks] == [
        (1, 7),
        (9, 9),
    ]


def test_four_space_indented_fence_is_not_a_fence() -> None:
    text = "Paragraph\n    ```\n    code\n    ```\nAfter"

    chunks = chunk_markdown("content.md", text)

    assert len(chunks) == 1
    assert chunks[0].text == text


def test_1201_character_block_is_hard_split_with_120_character_overlap() -> None:
    text = "x" * 1201

    chunks = chunk_markdown("content.md", text)

    assert [len(chunk.text) for chunk in chunks] == [1200, 121]
    assert chunks[0].text[-120:] == chunks[1].text[:120]
    assert chunks[0].text + chunks[1].text[120:] == text
    assert [(chunk.line_start, chunk.line_end) for chunk in chunks] == [(1, 1), (1, 1)]


def test_hard_split_reports_inclusive_source_line_ranges() -> None:
    text = "first\nsecond\nthird"

    chunks = chunk_markdown("nested/content.md", text, max_chars=8, overlap=2)

    assert [chunk.text for chunk in chunks] == ["first\nse", "second\nt", "\nthird"]
    assert [(chunk.line_start, chunk.line_end) for chunk in chunks] == [
        (1, 2),
        (2, 3),
        (2, 3),
    ]
    assert [chunk.ordinal for chunk in chunks] == [0, 1, 2]


def test_unicode_characters_are_counted_as_characters() -> None:
    chunks = chunk_markdown("中文/内容.md", "你好世界🙂", max_chars=4, overlap=1)

    assert [chunk.text for chunk in chunks] == ["你好世界", "界🙂"]
    assert [chunk.relative_path for chunk in chunks] == ["中文/内容.md", "中文/内容.md"]


def test_empty_file_returns_no_chunks() -> None:
    assert chunk_markdown("empty.md", "") == ()
    assert chunk_markdown("empty.md", "\n\n  \t") == ()


@pytest.mark.parametrize(
    ("max_chars", "overlap"),
    [(0, 0), (-1, 0), (10, -1), (10, 10), (10, 11)],
)
def test_invalid_chunk_sizes_are_rejected(max_chars: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        chunk_markdown("content.md", "text", max_chars=max_chars, overlap=overlap)


def test_repeated_calls_are_identical() -> None:
    text = "# Stable\n\nAlpha.\n\n~~~text\nbeta\n~~~"

    assert chunk_markdown("stable.md", text, max_chars=16, overlap=3) == chunk_markdown(
        "stable.md", text, max_chars=16, overlap=3
    )
