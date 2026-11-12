# Emacs agent-shell bridge

Load this bridge in an Emacs with `agent-shell` installed:

```elisp
(add-to-list 'load-path "/path/to/ai-agent-macropad/emacs")
(require 'agent-shell-macropad)
(agent-shell-macropad-mode 1)
```

The bridge subscribes to Agent Shell's lifecycle events. Each ACP
session has a stable `agent-shell:<session-id>` identity, so renaming
its buffer does not change its pad assignment. The daemon's M3 bank
shows these sessions, regardless of whether their backend is Claude,
Codex, or another ACP agent.

Input submission, tool activity, permission requests, turn completion,
errors, and cleanup use the same hook messages as the CLI adapters.
Prompts, streamed messages, and tool input are not forwarded. Native
CLI macropad hooks are suppressed inside ACP child processes so one
agent-shell session does not acquire a second CLI slot. This child
process marker takes effect for sessions started after enabling the
bridge.

Tapping a session's digit focuses its exact Emacs buffer. Square/X
allow once or deny its pending tool permission after that digit has
selected it. The bridge checks the original request ID, requires a
single pending request, and never substitutes an “allow always” option.
Form elicitations and ordinary questions still require their normal UI.
Switching source bank or page clears the selected approval target.

Missing daemon/socket access does not stop an agent session. On startup,
the daemon asks the live bridge to reannounce open sessions and their
current status. `agent-shell-macropad-resync` also does this manually;
a later lifecycle event can register a session if startup resync fails.
The daemon also polls `agent-shell-macropad-session-ids` to detect closed
buffers when a cleanup event was missed. This query does not change focus
or LED state; an unresponsive server leaves existing assignments intact.
`agent-shell-macropad-mode -1` unsubscribes and clears its assignments.

The implementation uses `agent-shell-subscribe-to`, with the ACP
client creation boundary marked through a scoped advice. Permission
responses use Agent Shell's existing permission response function;
check these integration points when upgrading Agent Shell.

Run the isolated tests through the existing Emacs server:

```elisp
(load "/path/to/ai-agent-macropad/emacs/agent-shell-macropad-tests.el")
(ert-run-tests-batch "^agent-shell-macropad-")
```
