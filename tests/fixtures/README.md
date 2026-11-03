# Hook contract fixture

`agy-pre-tool-use.json` transcribes the documented PreToolUse example at
https://www.antigravity.google/docs/hooks/ (retrieved 2026-10-26). The published
example identifiers are illustrative, not an operator session. Tests verify the
camelCase metadata and `toolCall.name` layout, then substitute the separately
documented `ask_question` tool to test the narrow WAIT transition. This does not
claim a real client emitted an event or that arbitrary tools imply permission WAIT.
