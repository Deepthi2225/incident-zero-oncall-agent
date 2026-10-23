"""The agent must never import or read the incident injector (it would know the answer)."""
import ast
from pathlib import Path

from oncall import config

AGENT_DIR = config.ROOT / "oncall"


def test_agent_never_references_incident_script():
    for path in AGENT_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not any(n.startswith("scripts") for n in names), f"{path} imports {names}"
        assert "incident.py" not in text and "scripts/" not in text, f"{path} mentions the injector"


def test_injector_only_uses_docker_stop_and_start():
    source = (config.ROOT / "scripts" / "incident.py").read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(source))
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "docker"]
    verbs = {c.args[0].value for c in calls if c.args and isinstance(c.args[0], ast.Constant)}
    assert verbs <= {"stop", "start", "inspect"}
    assert "flagd" not in source
