from pathlib import Path

ROOT = Path(".")


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


def test_readme_documents_chrome_extension_native_host():
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "autonomy-chrome-host" in text
    assert "chrome-extension" in text
    assert "native-host.example.json" in text
    assert "com.autonomy.app" in text
    assert "host/session count only" in text


def test_readme_documents_discord_dm_bot_optional_extra():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'discord = ["discord.py>=2,<3"]' in pyproject
    assert "## Discord DM Bot" in readme
    assert 'python -m pip install -e ".[discord]"' in readme
    assert "DISCORD_BOT_TOKEN" in readme
    assert "DISCORD_OWNER_ID" in readme
    assert "autonomy discord-bot --workspace . --max-steps 12" in readme


def test_readme_documents_telegram_dm_bot_optional_extra():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'telegram = ["python-telegram-bot>=22,<23"]' in pyproject
    assert "## Telegram DM Bot" in readme
    assert 'python -m pip install -e ".[telegram]"' in readme
    assert "TELEGRAM_BOT_TOKEN" in readme
    assert "TELEGRAM_OWNER_ID" in readme
    assert "autonomy telegram-bot --workspace . --max-steps 12" in readme


def test_readme_documents_delegate_toolset_as_implemented():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "`mcp`, and `delegate` toolsets" in readme
    assert "autonomy tools enable delegate" in readme
    assert "Explicit subagent requests expose `delegate.run`" in readme
    assert "planned Hermes-like toolsets such as\n`cronjob`" in readme
