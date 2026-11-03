# Paired-row fixture preview

The [live weather tiles](../docs/weather-tiles.md) originated here as an offline
design study. Runtime rendering now lives in `ulanzi/weather_tiles.py`; the daemon
does not import this experiment. This tool supplies explicit synthetic samples
and invokes the same visual renderer as the live widget path. It never opens a
device/socket, samples metrics or changes the running daemon.

Run on the deck host from the checkout:

```sh
.venv/bin/python -m experiments.weather_row --output /tmp/ulanzi-weather-row
.venv/bin/python -m pytest -q
```

This generates `quiet.png`, `busy.png` and `unknown.png`, each containing two rows
of 196×196 tiles. Conventional widgets sit directly above the corresponding
weather tile and consume the same fixture observations. These are examples,
not captures. Quiet activity leaves occupied memory and disk filled. Unknown
input remains N/A. The conventional disk tile uses its existing fullness bar.

For client access, copy through existing SSH/SFTP (run on the client, using your
configured SSH host alias for the deck host):

```sh
scp deckhost:/tmp/ulanzi-weather-row/busy.png ./ulanzi-busy.png
```

No browser service is needed. Temporary generated previews are reproducible
review artifacts owned by the generating lane and may be retired after visual
review. Source, tests and the deployment receipt are the durable result.
