"""Strip feature-flag data from anything read from the demo (HARD RULE 2).

Applied to every live answer before tools see it and again before anything is written to a
recorded bundle, so flag state never reaches the agent, fixtures or bundles. Removes:
  - dict keys starting with "feature_flag" (Prometheus labels, OpenSearch fields)
  - {"key": "feature_flag...", ...} tag/attribute entries and events named feature_flag*
  - log lines that mention feature_flag
"""
FLAG_PREFIX = "feature_flag"


def _is_flag(text):
    return str(text).lower().startswith(FLAG_PREFIX)


def strip_flags(obj):
    if isinstance(obj, dict):
        return {k: strip_flags(v) for k, v in obj.items() if not _is_flag(k)}
    if isinstance(obj, list):
        out = []
        for item in obj:
            if isinstance(item, dict) and (_is_flag(item.get("key", "")) or _is_flag(item.get("name", ""))):
                continue
            if isinstance(item, dict) and any(  # v1 Jaeger log entries: {"fields": [{"key": "event", "value": "feature_flag..."}]}
                    f.get("key") == "event" and _is_flag(f.get("value", ""))
                    for f in item.get("fields", []) if isinstance(f, dict)):
                continue
            out.append(strip_flags(item))
        return out
    if isinstance(obj, str) and FLAG_PREFIX in obj.lower() and "\n" in obj:
        return "\n".join(line for line in obj.splitlines() if FLAG_PREFIX not in line.lower())
    return obj


def contains_flags(obj):
    """True if any feature-flag data survives in obj (used by tests and before saving)."""
    if isinstance(obj, dict):
        return any(_is_flag(k) or contains_flags(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(contains_flags(v) for v in obj)
    if isinstance(obj, str):
        return FLAG_PREFIX in obj.lower()
    return False
