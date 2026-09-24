"""Features intentionally excluded from this fork's agent and dashboard."""

ARTIFACTS_ENABLED = False
MEMORY_ENABLED = False


def excluded_api_path(path: str) -> bool:
    """Reject legacy feature endpoints even when a caller knows their URLs."""
    return path.startswith((
        "/api/artifacts", "/api/artifact-folders", "/api/remote-artifacts",
        "/api/deploy",
        "/api/memory", "/api/lessons", "/api/learn", "/api/knowledge",
    ))
