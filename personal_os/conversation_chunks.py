"""Role-preserving chunks from the importer's role-prefixed transcript."""
import re


def split_conversation(text: str, size: int = 1800) -> list[str]:
    if size < 80:
        raise ValueError("Conversation chunk size must be at least 80")
    source = str(text or "").strip()
    if not source:
        return []
    boundaries = list(re.finditer(r"(?m)^(user|assistant|system|external):[ \t]*", source))
    if not boundaries:
        # Unattributed legacy text is never silently promoted to user Evidence.
        messages = [("external", source)]
    else:
        messages = []
        if source[:boundaries[0].start()].strip():
            messages.append(("external", source[:boundaries[0].start()].strip()))
        for index, match in enumerate(boundaries):
            end = boundaries[index + 1].start() if index + 1 < len(boundaries) else len(source)
            content = source[match.end():end].strip()
            if content:
                messages.append((match.group(1), content))
    pieces = []
    for role, content in messages:
        prefix = role + ": "
        budget = size - len(prefix)
        while len(content) > budget:
            boundary = max((m.end() for m in re.finditer(r"[。！？.!?]\s+|\n", content[:budget])), default=0)
            cut = boundary if boundary >= budget // 2 else budget
            pieces.append((role, prefix + content[:cut].strip()))
            content = content[cut:].lstrip()
        if content:
            pieces.append((role, prefix + content))
    chunks = []
    index = 0
    while index < len(pieces):
        role, part = pieces[index]
        if (role == "user" and index + 1 < len(pieces) and pieces[index + 1][0] == "assistant"
                and len(part) + 2 + len(pieces[index + 1][1]) <= size):
            part += "\n\n" + pieces[index + 1][1]
            index += 1
        chunks.append(part)
        index += 1
    return chunks
