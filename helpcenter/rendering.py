"""Turn an article body (plain text with "- " bullet lines) into safe HTML blocks, linking cross-references."""

import re

from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe


def link_references(text: str, links: dict[str, str]) -> str:
    """`text` is already escaped. Quoted titles of other articles ("Vaccine Prices") become links."""
    def replace(match):
        title = match.group(1)
        url = links.get(title)
        return format_html('"<a href="{}">{}</a>"', url, title) if url else match.group(0)

    return re.sub(r"&quot;([^&]+?)&quot;", replace, text)


def render_body(body: str, links: dict[str, str]) -> list:
    blocks = []
    for paragraph in (p.strip() for p in body.split("\n\n")):
        if not paragraph:
            continue
        lines = paragraph.splitlines()
        if all(line.lstrip().startswith("- ") for line in lines):
            items = "".join(f"<li>{link_references(escape(line.lstrip()[2:]), links)}</li>" for line in lines)
            blocks.append(mark_safe(f"<ul>{items}</ul>"))
        else:
            intro = [line for line in lines if not line.lstrip().startswith("- ")]
            bullets = [line for line in lines if line.lstrip().startswith("- ")]
            html = f"<p>{link_references(escape(' '.join(intro)), links)}</p>"
            if bullets:
                html += "<ul>" + "".join(f"<li>{link_references(escape(b.lstrip()[2:]), links)}</li>" for b in bullets) + "</ul>"
            blocks.append(mark_safe(html))
    return blocks
