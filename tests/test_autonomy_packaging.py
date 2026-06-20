from pathlib import Path


def test_requirements_installs_local_checkout_without_pinned_git_url():
    text = Path("requirements.txt").read_text(encoding="utf-8")

    assert "git+" not in text
    assert "-e .[dev]" in text


def test_readme_has_fresh_clone_quickstart_for_mac_and_linux():
    text = Path("README.md").read_text(encoding="utf-8")

    assert "## Quickstart: macOS and Linux" in text
    assert "python3.13 -m venv .venv" in text
    assert 'python -m pip install -e ".[dev]"' in text
    assert "autonomy model setup" in text
    assert "autonomy doctor" in text


def test_workspace_runtime_state_is_gitignored():
    text = Path(".gitignore").read_text(encoding="utf-8")

    assert ".autonomy/" in text
