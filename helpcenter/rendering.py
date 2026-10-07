"""Turn an article body (plain text with "- " bullet lines) into safe HTML blocks, linking cross-references."""

import re

from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe


def link_references(text: str, links: dict[str, str]) -> str:
    """Escape `text`, turning quoted titles of other articles ("Vaccine Prices") into links. Matched on the raw
    text: once escaped, a title with "&" or an apostrophe ("Fleas & Ticks") no longer matches its key."""
    html = []
    for i, part in enumerate(re.split(r'"([^"]+)"', text)):  # outside, quoted, outside, quoted, ..., outside
        if i % 2 == 0:
            html.append(escape(part))
        elif part in links:
            html.append(format_html('"<a href="{}">{}</a>"', links[part], part))
        else:
            html.append(escape(f'"{part}"'))
    return "".join(html)


def render_body(body: str, links: dict[str, str]) -> list:
    blocks = []
    for paragraph in (p.strip() for p in body.split("\n\n")):
        if not paragraph:
            continue
        lines = paragraph.splitlines()
        if all(line.lstrip().startswith("- ") for line in lines):
            items = "".join(f"<li>{link_references(line.lstrip()[2:], links)}</li>" for line in lines)
            blocks.append(mark_safe(f"<ul>{items}</ul>"))
        else:
            intro = [line for line in lines if not line.lstrip().startswith("- ")]
            bullets = [line for line in lines if line.lstrip().startswith("- ")]
            html = f"<p>{link_references(' '.join(intro), links)}</p>"
            if bullets:
                html += (
                    "<ul>" + "".join(f"<li>{link_references(b.lstrip()[2:], links)}</li>" for b in bullets) + "</ul>"
                )
            blocks.append(mark_safe(html))
    return blocks
