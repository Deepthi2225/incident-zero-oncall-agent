"""HARD RULE 2 enforcement: refuse any URL, path or command that touches feature-flag state."""
from oncall import config


class ForbiddenAccess(RuntimeError):
    pass


def check(target):
    """Raise ForbiddenAccess if target (URL, path or command string) is off-limits."""
    lowered = str(target).lower()
    for bad in config.FORBIDDEN_SUBSTRINGS:
        if bad in lowered:
            raise ForbiddenAccess(f"blocked by HARD RULE 2 (feature-flag state): {target}")
    return target
