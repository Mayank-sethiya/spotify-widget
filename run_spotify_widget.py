#!/usr/bin/env python3
"""
Windows launcher for the Spotify Widget.

What it does on first run:
- Checks Python version (3.9+ recommended)
- Creates a .venv next to this file
- Installs dependencies from requirements.txt
- Relaunches itself inside the venv
- Imports and runs spotify_widget.main()

Usage:
- Double-click this file in Explorer, or
- Run: py run_spotify_widget.py
"""

from __future__ import annotations

import os
import sys
import subprocess
import venv
import traceback
from pathlib import Path
from typing import Optional, Dict, List, Tuple

MIN_PYTHON = (3, 9)
ENV_FLAG = "SPOTIFY_WIDGET_BOOTSTRAPPED"
LOG_FILE = "spotify_widget_bootstrap.log"


def is_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def pause_on_exit():
    # If launched by double-click on Windows, keep the window open on error
    if os.name == "nt" and not is_tty():
        try:
            input("\nPress Enter to exit...")
        except Exception:
            pass


def log_path(base_dir: Path) -> Path:
    return base_dir / LOG_FILE


def log(msg: str, base_dir: Path) -> None:
    try:
        with log_path(base_dir).open("a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")
    except Exception:
        pass


def ensure_python_version():
    if sys.version_info < MIN_PYTHON:
        print(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required. "
            f"Current: {sys.version.split()[0]}",
            file=sys.stderr,
        )
        pause_on_exit()
        sys.exit(1)


def venv_paths(venv_dir: Path) -> Tuple[Path, Path]:
    if os.name == "nt":
        py = venv_dir / "Scripts" / "python.exe"
        pip = venv_dir / "Scripts" / "pip.exe"
    else:
        py = venv_dir / "bin" / "python"
        pip = venv_dir / "bin" / "pip"
    return py, pip


def create_venv(venv_dir: Path) -> None:
    builder = venv.EnvBuilder(with_pip=True, clear=False, upgrade=False)
    builder.create(str(venv_dir))


def run_cmd(cmd: List[str], env: Optional[Dict[str, str]] = None, cwd: Optional[Path] = None) -> None:
    subprocess.check_call(cmd, env=env, cwd=str(cwd) if cwd else None)


def install_requirements(venv_python: Path, base_dir: Path) -> None:
    req = base_dir / "requirements.txt"
    run_cmd([str(venv_python), "-m", "pip", "install", "--upgrade", "pip", "wheel", "setuptools"])
    if req.exists():
        print("Installing dependencies from requirements.txt ...")
        run_cmd([str(venv_python), "-m", "pip", "install", "-r", str(req)])
    else:
        print("Warning: requirements.txt not found; skipping dependency installation.")


def relaunch_in_venv(venv_python: Path, script_path: Path):
    env = os.environ.copy()
    env[ENV_FLAG] = "1"
    args = [str(venv_python), str(script_path), *sys.argv[1:]]
    os.execvpe(str(venv_python), args, env)


def import_and_run(base_dir: Path) -> None:
    # Ensure repo root (where spotify_widget.py lives) is on sys.path
    if str(base_dir) not in sys.path:
        sys.path.insert(0, str(base_dir))

    try:
        import spotify_widget  # your main module file is spotify_widget.py
    except Exception as e:
        print("Could not import spotify_widget.py. Make sure this file is next to run_spotify_widget.py.", file=sys.stderr)
        print(f"Import error: {e}", file=sys.stderr)
        raise

    # Call its main() entrypoint
    try:
        if hasattr(spotify_widget, "main") and callable(spotify_widget.main):
            spotify_widget.main()
        else:
            raise AttributeError("spotify_widget.main not found or not callable.")
    except Exception:
        raise


def main():
    ensure_python_version()

    script_path = Path(__file__).resolve()
    base_dir = script_path.parent
    venv_dir = base_dir / ".venv"
    venv_python, _ = venv_paths(venv_dir)

    try:
        if os.environ.get(ENV_FLAG) != "1":
            print("Setting up the environment (first run may take a minute)...")
            log("Bootstrapping environment...", base_dir)

            if not venv_dir.exists():
                print(f"Creating virtual environment at {venv_dir} ...")
                create_venv(venv_dir)

            if not venv_python.exists():
                raise RuntimeError("Virtual environment Python not found after creation.")

            install_requirements(venv_python, base_dir)

            print("Relaunching inside the virtual environment...")
            relaunch_in_venv(venv_python, script_path)
            return

        # Already in venv: run the app
        import_and_run(base_dir)

    except subprocess.CalledProcessError as e:
        msg = f"Command failed: {e}\n"
        print(msg, file=sys.stderr)
        log(msg, base_dir)
        traceback.print_exc()
        pause_on_exit()
        sys.exit(e.returncode or 1)
    except Exception as e:
        msg = f"Error: {e}\n"
        print(msg, file=sys.stderr)
        log(msg, base_dir)
        traceback.print_exc()
        pause_on_exit()
        sys.exit(1)


if __name__ == "__main__":
    main()