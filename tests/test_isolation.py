"""The agent must never import, read or call the fault injector (it would know the answer)."""
import ast
import os

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


SCRIPTS_DIR = config.ROOT / "scripts"


def test_nothing_runs_a_faultctl_command():
    """oncall/ and scripts/ never start a `faultctl` process: the organisers' tool has the same
    name, and a bare command (or `-m faultctl` from another directory) could reach theirs."""
    for path in [*AGENT_DIR.rglob("*.py"), *SCRIPTS_DIR.glob("*.py")]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in (
                    "run", "Popen", "call", "check_call", "check_output", "system"):
                text = ast.unparse(node)
                assert "faultctl" not in text, f"{path.name}: {text[:120]}"


def test_scripts_only_drive_this_repos_faultctl():
    """Scripts that import faultctl refuse to run unless it resolves to this repo's harness."""
    for path in SCRIPTS_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "import faultctl" in text:
            assert "refusing to run: 'faultctl' resolved to" in text, path.name


def test_scripts_use_the_scoped_reset():
    """demo.py and the audit undo only what faultctl injected, never a project-wide sweep."""
    for name in ("demo.py", "recovery_audit.py"):
        text = (SCRIPTS_DIR / name).read_text(encoding="utf-8")
        calls = text.count("faultctl.reset(")
        assert calls and text.count("faultctl.reset(everything=False)") == calls, name


READ_ONLY_DOCKER = {"ps", "inspect", "stats", "logs", "port"}
ACTIONAPI_DIR = config.ROOT / "actionapi"


def test_agent_never_runs_state_changing_docker_commands():
    """The agent only observes. Every docker command in oncall/ goes through sources/live._docker,
    and every _docker call uses a read-only verb; changes go through the Action API over HTTP."""
    builders = []
    for path in AGENT_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_docker":
                verb = node.args[0]
                assert isinstance(verb, ast.Constant) and verb.value in READ_ONLY_DOCKER, \
                    f"{path.name}: docker {ast.unparse(verb)}"
            if isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant) \
                    and node.elts[0].value == "docker":
                builders.append(f"{path.relative_to(AGENT_DIR)}:{node.lineno}")
    assert builders == [f"sources{os.sep}live.py:{_live_docker_line()}"], builders


def _live_docker_line():
    tree = ast.parse((AGENT_DIR / "sources" / "live.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_docker")
    return next(n.lineno for n in ast.walk(fn) if isinstance(n, ast.List))


def test_agent_and_action_api_are_separate():
    """The agent reaches the Action API only over HTTP; the API never imports the agent."""
    for path in AGENT_DIR.rglob("*.py"):
        mods = list(_imports(ast.parse(path.read_text(encoding="utf-8"))))
        assert not any(m == "actionapi" or m.startswith("actionapi.") for m in mods), path.name
    for path in ACTIONAPI_DIR.rglob("*.py"):
        mods = list(_imports(ast.parse(path.read_text(encoding="utf-8"))))
        assert not any(m.split(".")[0] in ("oncall", "faultctl", "detector") for m in mods), path.name


def test_guard_allows_the_local_action_api_and_nothing_else_new():
    assert config.FORBIDDEN_SUBSTRINGS == ["/feature", "/flagservice", ":8013", ":8016", "flagd",
                                           "demo.flagd.json", "faultctl"]
    for path in ("restart_service", "throttle_load_generator", "unthrottle_load_generator",
                 "toggle_feature_flag", "scale_service"):
        check(f"http://127.0.0.1:8099/actions/{path}")  # does not raise
    for url in ("http://127.0.0.1:8013/x", "http://localhost:8080/feature", "http://127.0.0.1:8099/flagservice"):
        with pytest.raises(ForbiddenAccess):
            check(url)


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
