# Reporting a vulnerability

The maintained version is the current default branch. There are no separately
supported release lines yet.

Use GitHub's **Report a vulnerability** option on this repository's Security tab
when available. If it is unavailable, open an issue asking for a private contact
channel without including sensitive details. Do not put credentials, private
logs or unpublished vulnerability details in a public issue.

Include the affected version, reproduction steps, expected behavior and impact.
No response-time guarantee is offered.

The daemon runs as your user, and configured key commands execute shell commands
with that user's permissions. Only load configuration you trust. The control
interface is a local Unix socket; remote use relies on your existing SSH access.
Agent attention buttons report observations and never approve an action.
