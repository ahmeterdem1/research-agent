"""
Antigravity `read_file`-style line-range pagination, heading outline extraction,
and HTML boilerplate stripping for `ra_search.read_online_document` and `ra_docs.get_document`.
"""

import re
from typing import Optional

DEFAULT_MAX_LINES_PER_READ = 400
DEFAULT_MAX_CHARS_PER_READ = 35_000
MAX_OUTLINE_HEADINGS = 45

_HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def extract_clean_html_markdown(html: str) -> str:
    """
    Strip navigation/script/style boilerplate from raw HTML, convert `<h1>`–`<h6>` tags
    into Markdown `#`–`######` headings, and return clean line-oriented text.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    # Remove non-content / boilerplate elements
    for tag in soup.find_all(
        ["script", "style", "noscript", "svg", "iframe", "nav", "footer", "header", "aside", "form"]
    ):
        tag.decompose()

    # Convert HTML headings (h1..h6) into Markdown heading lines so outline extraction works
    for level in range(1, 7):
        prefix = "#" * level
        for h_tag in soup.find_all(f"h{level}"):
            heading_txt = re.sub(r"\s+", " ", h_tag.get_text(" ", strip=True)).strip()
            if heading_txt:
                h_tag.replace_with(f"\n{prefix} {heading_txt}\n")

    # Prefer <article> or <main> if it holds substantive content; otherwise fall back to full soup
    root = soup
    for candidate_tag in ("article", "main"):
        found = soup.find(candidate_tag)
        if found and len(found.get_text(strip=True)) >= 200:
            root = found
            break

    raw_text = root.get_text(separator="\n", strip=True)
    cleaned_lines: list[str] = []
    blank_streak = 0
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            blank_streak += 1
            if blank_streak <= 1:
                cleaned_lines.append("")
        else:
            blank_streak = 0
            cleaned_lines.append(stripped)

    return "\n".join(cleaned_lines).strip()


def extract_document_outline(lines: list[str], max_items: int = MAX_OUTLINE_HEADINGS) -> list[str]:
    """Return a list of `L<line_no>: <heading>` strings for Markdown headings in `lines`."""
    outline: list[str] = []
    for idx, line in enumerate(lines, start=1):
        m = _HEADING_LINE_RE.match(line.strip())
        if m:
            hashes, title = m.group(1), m.group(2).strip()
            if title:
                outline.append(f"  - L{idx}: {hashes} {title[:110]}")
                if len(outline) >= max_items:
                    outline.append("  - ... (additional headings omitted)")
                    break
    return outline


def paginate_document_text(
    text: str,
    source_label: str,
    tool_name: str,
    tool_arg_hint: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    max_lines_per_read: int = DEFAULT_MAX_LINES_PER_READ,
    max_chars_per_read: int = DEFAULT_MAX_CHARS_PER_READ,
) -> str:
    """
    Format and slice `text` by 1-based inclusive `[start_line, end_line]` with a
    Document Outline (Headings & Line Numbers) and explicit pagination instructions.
    """
    if not text or not text.strip():
        return f"Empty document: {source_label}"

    lines = text.splitlines()
    total_lines = len(lines)
    total_chars = len(text)

    s_line = max(1, int(start_line)) if start_line is not None else 1
    if s_line > total_lines:
        return (
            f"Requested start_line={s_line} exceeds document length ({total_lines} lines) for {source_label}.\n"
            f"Valid line range is 1 to {total_lines}."
        )

    if end_line is not None:
        requested_end = min(total_lines, max(s_line, int(end_line)))
        target_end = min(requested_end, s_line + max_lines_per_read - 1)
    else:
        target_end = min(total_lines, s_line + max_lines_per_read - 1)

    selected_lines: list[str] = []
    acc_chars = 0
    actual_end = s_line - 1
    truncated_by_chars = False

    for line_no in range(s_line, target_end + 1):
        raw_line = lines[line_no - 1]
        formatted_line = f"{line_no}: {raw_line}"
        if selected_lines and (acc_chars + len(formatted_line) + 1 > max_chars_per_read):
            truncated_by_chars = True
            break
        selected_lines.append(formatted_line)
        acc_chars += len(formatted_line) + 1
        actual_end = line_no

    header_parts: list[str] = [
        f"Showing lines {s_line}-{actual_end} of {total_lines:,} ({total_chars:,} chars total) from {source_label}"
    ]

    outline = extract_document_outline(lines)
    if outline and (s_line == 1 or total_lines > max_lines_per_read):
        header_parts.append("Document Outline (Headings & Line Numbers):\n" + "\n".join(outline))

    body = "\n".join(selected_lines)

    if actual_end < total_lines:
        next_start = actual_end + 1
        next_end = min(total_lines, next_start + max_lines_per_read - 1)
        reason_note = " (reached per-call character limit)" if truncated_by_chars else ""
        footer = (
            f"[Document continues{reason_note}: lines {next_start}-{total_lines} remaining. "
            f"To read further, call `{tool_name}({tool_arg_hint}, start_line={next_start}, end_line={next_end})` "
            f"or jump directly to any heading line number from the Document Outline above.]"
        )
    else:
        footer = (
            f"[End of document reached (line {actual_end} of {total_lines}). "
            f"All content up to line {total_lines} has been shown.]"
        )

    return "\n\n".join(header_parts) + "\n---\n" + body + "\n---\n" + footer

