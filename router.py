CONTROL_ALIASES = {
    "/daemon status": "/service status",
    "/daemon restart": "/restart",
    "/service restart": "/restart",
    "/task list": "/tasks",
}


def normalize_command(content: str) -> str:
    normalized = content.strip().lower()
    return CONTROL_ALIASES.get(normalized, normalized)


def extract_search_directives(content: str) -> tuple[str, bool, bool]:
    text = content.strip()
    if text.startswith("/web "):
        return text[5:].strip(), True, False
    if text.startswith("/local "):
        return text[7:].strip(), False, True
    return text, False, False


def command_arg(content: str) -> str:
    parts = content.strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""
