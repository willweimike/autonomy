Status
- Done. Implemented `ChromeApprovalBroker`, added `approval.respond` handling, installed `ApprovalPolicy(prompt=broker.prompt)` through the Chrome session agent-loop factory, and flushed queued `approval.requested` host events before the final response.

Approach
- Followed the task brief verbatim and kept the change on the existing seams: `ApprovalPolicy`, `ChromeSessionBridge`, and the native message host loop.
- Used TDD on the approval broker and host event flow:
  - added failing broker tests first and verified the expected `ImportError`
  - added failing host checks for accepting `approval.respond` and emitting queued events before the final response
  - implemented the minimum bridge code to satisfy those tests

Changed files
- `autonomy/chrome_api.py`
  - added `ChromeApprovalBroker`
  - queued outbound approval events on the session bridge
  - handled inbound `approval.respond`
  - installed broker-backed `ApprovalPolicy` on real agent loops
- `autonomy/chrome_host.py`
  - accepted `approval.respond` as a native request type
  - added `NativeMessageWriter` with a lock
  - flushed queued bridge events before the per-request response
- `tests/test_autonomy_native_chrome_host.py`
  - added approval broker allow/deny/timeout coverage
  - added native host approval request-type coverage
  - added queued host event ordering coverage

Verification
- Red:
  - `python3.13 -m pytest tests/test_autonomy_native_chrome_host.py::AutonomyNativeChromeApprovalTest -q`
  - result: failed with `ImportError: cannot import name 'ChromeApprovalBroker'`
- Green:
  - `python3.13 -m pytest tests/test_autonomy_native_chrome_host.py::AutonomyNativeChromeApprovalTest -q`
  - result: `2 passed`
  - `python3.13 -m pytest tests/test_autonomy_native_chrome_host.py -q`
  - result: `16 passed`
- Sanity:
  - `git diff --check`
  - result: clean

Commit
- `5f9ca17a17be2bc5f8ebcd766d470470bfcc0855` `feat: add chrome approval bridge`

Concerns
- Disconnect still resolves by timeout-to-deny rather than an immediate pending-request drain. That matches the brief’s default-deny requirement, but it is still timeout-based behavior.

Follow-up fix
- Reviewer finding reproduced at the native host boundary: `run_chrome_host()` still handled `chat.send` synchronously, so a pending `ChromeApprovalBroker.prompt()` could emit `approval.requested` but the host could not ingest `approval.respond` until the same `chat.send` call unwound.
- Kept the change minimal and on the preferred shape:
  - `ChromeSessionBridge` now supports an optional live `event_sink`; when present, approval events are written immediately instead of only being queued.
  - `run_chrome_host()` installs `NativeMessageWriter.send` as that sink, runs `chat.send` requests in a worker thread, and leaves the main native-message loop free to read `approval.respond`.
  - Worker threads write their eventual `chat.result` through the same writer lock, and the host waits for outstanding workers on EOF so the final response is not dropped.
- Added a focused regression test at the native-host layer:
  - `test_chrome_host_processes_approval_while_chat_send_is_blocked`
  - It proves message ordering is `approval.requested` -> `approval.result` -> `chat.result` when `approval.respond` arrives while the original `chat.send` is still blocked.

Verification
- `python3.13 -m pytest tests/test_autonomy_native_chrome_host.py::AutonomyNativeChromeApprovalTest -q` -> `2 passed`
- `python3.13 -m pytest tests/test_autonomy_native_chrome_host.py -q` -> `17 passed`
- `git diff --check` -> clean
