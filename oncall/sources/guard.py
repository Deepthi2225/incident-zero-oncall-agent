"""HARD RULE 2 enforcement: refuse any URL, path or command that touches feature-flag state."""
from oncall import config


class ForbiddenAccess(RuntimeError):
    pass


def allowed(target):
    """True if target is not feature-flag related (used to filter container lists)."""
    lowered = str(target).lower()
    return not any(bad in lowered for bad in config.FORBIDDEN_SUBSTRINGS)


def check(target):
    """Raise ForbiddenAccess if target (URL, path or command string) is off-limits."""
    lowered = str(target).lower()
    for bad in config.FORBIDDEN_SUBSTRINGS:
        if bad in lowered:
            raise ForbiddenAccess(f"blocked by HARD RULE 2 (feature-flag state): {target}")
    return target
