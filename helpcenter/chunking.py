def split_into_chunks(title: str, body: str, max_chars: int = 700) -> list[str]:
    """Greedy paragraph packing. Each chunk is prefixed with the article title so it stays meaningful alone."""
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    chunks, current = [], ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 2 > max_chars:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        chunks.append(current)
    return [f"{title}\n\n{chunk}" for chunk in chunks]
