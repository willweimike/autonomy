Status: DONE
Commit(s): c589e8e
One-line test summary: `python3.13 -m pytest tests/test_autonomy_native_chrome_host.py -q`, `python3.13 -m pytest tests/test_autonomy_native_cli.py -q`, and `python3.13 -m autonomy chrome-host < /dev/null` all passed.
Concerns: none
Report file path: /Users/awei/Documents/GitHub/autonomy/.worktrees/chrome-extension-bridge/.superpowers/sdd/task-1-report.md

Fix report:
- Scoped Task 1 native-host validation back to `status` only in `autonomy/chrome_host.py`; future request types stay out of this layer.
- Added explicit protocol reject tests for missing `type`, unknown `type`, malformed JSON, invalid UTF-8, truncated header, and truncated body in `tests/test_autonomy_native_chrome_host.py`.
- Moved the `chrome_host` import in `autonomy/cli.py` into the `chrome-host` branch so module import stays lazy.
- Verified with `python3.13 -m pytest tests/test_autonomy_native_chrome_host.py -q`, `python3.13 -m pytest tests/test_autonomy_native_cli.py -q`, and `python3.13 -m autonomy chrome-host < /dev/null`.

Fix report update:
- Removed the startup `Path.cwd()` directory validation from `run_chrome_host()` so Task 1 stays limited to native framing and `status` echo behavior.
- Dropped the now-unused `Path` import from `autonomy/chrome_host.py`.
