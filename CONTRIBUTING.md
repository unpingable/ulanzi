# Contributing

Bug reports and focused patches are welcome through this repository's GitHub
issues and pull requests. Explain the behavior you expected, what happened, and
how to reproduce it. For hardware reports, include Linux/Python versions, D200
firmware version if known, and whether the issue affects one or several devices.
Remove serial numbers, usernames, local paths, credentials and private command
output before posting logs or captures. See [SECURITY.md](SECURITY.md) for
confidential reports.

## Local checks

From the source checkout, with Python 3.11 or newer:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/ulanzid --config buttons.toml --check
```

Tests use temporary Unix sockets and fake USB devices. They do not need a deck,
root, agent credentials or a running daemon. Environments that prohibit Unix
sockets cannot run the entire suite. The optional strmdck framing comparison is
skipped unless `requirements-reference.txt` is installed.

To check distributable packaging:

```sh
.venv/bin/python -m pip install build
.venv/bin/python -m build
```

The wheel must include the config, helper scripts, hook examples, service/udev
templates and all three license/notice files. Do not publish a package as part of
a routine contribution.

Hardware checks under `qualify/` require exclusive device access and a physical
operator. Do not run them against a controller someone is using. Review the
[hardware summary](docs/qualification/README.md) before making claims; successful
host writes do not establish that the display or input worked. Keep raw receipts
private and publish only reviewed summaries.

## Scope and licensing

Keep device framing separate from rendering and agent event handling. Preserve
one reply per socket request, user-facing key numbers 1–13, bounded icon names,
and the existing heartbeat limits. The daemon does not open a network listener.
Contributions are provided under [Apache-2.0](LICENSE); retain applicable upstream
copyright and license notices.
