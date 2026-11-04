"""Guards for the article and its figures: every image exists, has alt text, and carries no metadata."""
import re
import struct
from pathlib import Path

ARTICLE = Path(__file__).resolve().parent.parent / "docs" / "article" / "article.md"
ALLOWED_CHUNKS = {b"IHDR", b"PLTE", b"IDAT", b"IEND"}  # anything else (tEXt, eXIf, iTXt, tIME, pHYs...) can carry metadata


def images():
    return re.findall(r"!\[([^\]]*)\]\(([^)]+)\)", ARTICLE.read_text())


def png_chunks(path: Path) -> list[bytes]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, out = 8, []
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        out.append(data[pos + 4:pos + 8])
        pos += 12 + length
    return out


def test_every_image_exists_and_has_real_alt_text():
    found = images()
    assert len(found) >= 5
    for alt, rel in found:
        assert (ARTICLE.parent / rel).is_file(), rel
        assert len(alt.split()) >= 8, f"alt text too short for {rel}"


def test_figures_carry_no_metadata():
    for _, rel in images():
        assert set(png_chunks(ARTICLE.parent / rel)) <= ALLOWED_CHUNKS, rel


def test_every_figure_file_is_used_in_the_article():
    used = {Path(rel).name for _, rel in images()}
    assert {p.name for p in (ARTICLE.parent / "figures").glob("*.png")} == used


def test_article_length_is_in_the_planned_range():
    prose = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", ARTICLE.read_text())  # alt text is not read as prose
    words = len(prose.split())
    assert 1500 <= words <= 2600


def test_house_style_no_bold_text():
    assert "**" not in ARTICLE.read_text()  # the author does not use bold in articles


def test_readme_links_the_published_story_without_tracking_parameters():
    readme = (ARTICLE.parents[2] / "README.md").read_text()
    links = re.findall(r"https://medium\.com/[^\s)]+", readme)
    assert len(links) == 1 and "?" not in links[0] and "building-a-work-briefing-that-remembers" in links[0]
