"""The agent must never import, read or call the fault injector (it would know the answer)."""
import ast

import pytest

from oncall import config
from oncall.sources.guard import ForbiddenAccess, check

AGENT_DIR = config.ROOT / "oncall"
FAULTCTL_DIR = config.ROOT / "faultctl"


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


def _strings_outside_guard_list(tree):
    """String constants, except the guard's own FORBIDDEN_SUBSTRINGS list (which names faultctl)."""
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "FORBIDDEN_SUBSTRINGS" for t in node.targets):
            skip |= {id(n) for n in ast.walk(node.value)}
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip]


@pytest.mark.parametrize("path", sorted(AGENT_DIR.rglob("*.py")), ids=lambda p: p.name)
def test_agent_never_references_fault_injection(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for name in _imports(tree):
        assert not name.startswith(("faultctl", "scripts")), f"{path} imports {name}"
    for text in _strings_outside_guard_list(tree):
        assert "faultctl" not in text and "incident.py" not in text, f"{path}: {text[:80]!r}"


def test_guard_blocks_faultctl():
    for target in ("faultctl/state.json", "python -m faultctl status"):
        with pytest.raises(ForbiddenAccess):
            check(target)


def test_faultctl_does_not_depend_on_the_agent():
    for path in FAULTCTL_DIR.rglob("*.py"):
        for name in _imports(ast.parse(path.read_text(encoding="utf-8"))):
            assert not name.startswith("oncall"), f"{path} imports {name}"


def test_faultctl_only_uses_docker_lifecycle_and_resource_commands():
    source = (FAULTCTL_DIR / "core.py").read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(source))
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "docker"]
    verbs = {c.args[0].value for c in calls if c.args and isinstance(c.args[0], ast.Constant)}
    assert verbs <= {"stop", "start", "kill", "inspect", "stats", "update", "exec", "info", "ps"}
    assert "flagd" not in source and "opentelemetry-demo" not in source  # no flags, no demo files
