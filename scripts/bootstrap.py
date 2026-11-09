"""First-time setup on any machine: python scripts/bootstrap.py

Standard library only, so it runs before anything is installed. It creates .venv, installs the
requirements, creates .env if there is none, and fills in a random Action API token. It never
overwrites a setting you already have.
"""
import re
import secrets
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MINIMUM = (3, 11)


def venv_python():
    return ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def fill_in(text, name, value):
    """text with name=value set, unless the setting already has a value."""
    if re.search(rf"^{name}=\S", text, re.M):
        return text
    if re.search(rf"^#?\s*{name}=.*$", text, re.M):
        return re.sub(rf"^#?\s*{name}=.*$", f"{name}={value}", text, count=1, flags=re.M)
    return text.rstrip("\n") + f"\n{name}={value}\n"


def make_env():
    env, example = ROOT / ".env", ROOT / ".env.example"
    created = not env.exists()
    if created:
        shutil.copy(example, env)
    text = env.read_text(encoding="utf-8")
    text = fill_in(text, "ACTION_API_TOKEN", secrets.token_urlsafe(32))
    text = fill_in(text, "ACTION_API_URL", "http://127.0.0.1:8099")
    env.write_text(text, encoding="utf-8")
    return created


def main():
    if sys.version_info < MINIMUM:
        sys.exit(f"P0ppy needs Python {MINIMUM[0]}.{MINIMUM[1]} or newer; this is {sys.version.split()[0]}.")
    if not venv_python().exists():
        print("Creating .venv ...", flush=True)
        venv.create(ROOT / ".venv", with_pip=True)
    print("Installing requirements ...", flush=True)
    subprocess.run([str(venv_python()), "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")], check=True)
    created = make_env()
    print(".env created." if created else ".env already exists; your settings were kept.")
    activate = r".venv\Scripts\Activate.ps1" if sys.platform == "win32" else "source .venv/bin/activate"
    print(f"""
Setup finished. Next:
  1. Add an LLM key to .env (for example ONCALL_PROVIDER=groq and GROQ_API_KEY=...).
  2. Activate the environment:  {activate}
  3. See what is left to do:    python -m oncall doctor
""")


if __name__ == "__main__":
    main()
