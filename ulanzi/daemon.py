#!/usr/bin/env python3
"""Ulanzi D200 daemon: keys run shell commands, a Unix socket sets labels, and
keys can show live local metrics."""
from __future__ import annotations

import asyncio
import argparse
import fcntl
import importlib.metadata
import json
import math
import copy
import os
import signal
import socket as socket_module
import stat
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ulanzi import deckproto
from ulanzi import deckzip
from ulanzi import metrics
from ulanzi import tiles

# People see keys 1..13 (row by row) and 'window'; the protocol's 0-based indices
# stay inside the daemon and appear only as raw= in the log.
KEY_COUNT = 13     # keys with a display
WINDOW_INDEX = 13  # the small info window is a touch area; it can run a command
WINDOW_NAME = 'window'

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO / 'buttons.toml'
RECONNECT_DELAY = 2.0
PUSH_DEBOUNCE = 0.3
TILE_LOG_EVERY = 300.0  # tile uploads are summarized, not logged one by one

# With no host write for this long the firmware drops the layout and shows its
# standalone splash. A tested unit measured ~11-13 s (docs/qualification); other
# projects report ~5 s. Heartbeat limits are checked against the shorter figure.
FIRMWARE_WATCHDOG = 5.0
DEFAULT_HEARTBEAT = 2.0
HEARTBEAT_RANGE = (0.5, 3.0)
DEFAULT_REFRESH = 2.0
REFRESH_RANGE = (1.0, 60.0)
DEFAULT_CLOCK = 1.0
CLOCK_RANGE = (0.0, 60.0)  # 0 turns host clock updates off


def log(*args):
    print('ulanzid:', *args, file=sys.stderr, flush=True)


class ConfigError(Exception):
    pass


@dataclass
class Key:
    label: str = ''
    widget: str | None = None
    run: str | None = None
    show_active: bool = True


@dataclass
class Config:
    brightness: int = 60
    heartbeat: float = DEFAULT_HEARTBEAT
    refresh: float = DEFAULT_REFRESH
    clock: float = DEFAULT_CLOCK
    keys: dict[int, Key] = field(default_factory=dict)

    def widget_keys(self) -> dict[int, str]:
        return {n: k.widget for n, k in self.keys.items() if k.widget}


def parse_key(name, window_ok=False) -> int:
    """Internal index from a user-facing key name: '1'..'13', or 'window'."""
    if window_ok and name == WINDOW_NAME:
        return WINDOW_INDEX
    try:
        n = int(name)
    except (TypeError, ValueError):
        n = None
    if n is None or not 1 <= n <= KEY_COUNT:
        also = f" or '{WINDOW_NAME}'" if window_ok else ''
        raise ConfigError(f'key {name!r} is not 1..{KEY_COUNT}{also}')
    return n - 1


def key_name(n: int) -> str:
    return WINDOW_NAME if n == WINDOW_INDEX else str(n + 1)


def _seconds(deck: dict, name: str, default: float, bounds: tuple[float, float], why: str = '') -> float:
    value = deck.get(name, default)
    low, high = bounds
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ConfigError(f'deck.{name} must be seconds in {low:g}..{high:g}{why}')
    return float(value)


def load_config(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f'{path}: {e}') from None

    return parse_config(data)


def parse_config(data: dict) -> Config:
    if not isinstance(data, dict) or not isinstance(data.get('deck', {}), dict) or not isinstance(data.get('keys', {}), dict):
        raise ConfigError('deck and keys must be tables')
    deck = data.get('deck', {})
    unknown = set(deck) - {'brightness', 'heartbeat', 'refresh', 'clock'}
    if unknown:
        raise ConfigError(f'deck: unknown fields {sorted(unknown)}')
    brightness = deck.get('brightness', 60)
    if isinstance(brightness, bool) or not isinstance(brightness, int) or not 0 <= brightness <= 100:
        raise ConfigError('deck.brightness must be an integer 0..100')
    heartbeat = _seconds(deck, 'heartbeat', DEFAULT_HEARTBEAT, HEARTBEAT_RANGE,
                         f', well under the ~{FIRMWARE_WATCHDOG:g} s firmware watchdog')
    refresh = _seconds(deck, 'refresh', DEFAULT_REFRESH, REFRESH_RANGE)
    clock = _seconds(deck, 'clock', DEFAULT_CLOCK, CLOCK_RANGE)

    keys = {}
    for raw, spec in data.get('keys', {}).items():
        n = parse_key(raw, window_ok=True)
        if not isinstance(spec, dict):
            raise ConfigError(f'keys.{raw} must be a table')
        unknown = set(spec) - {'label', 'widget', 'run', 'show_active'}
        if unknown:
            raise ConfigError(f'keys.{raw}: unknown fields {sorted(unknown)}')
        label, widget, run = spec.get('label', ''), spec.get('widget'), spec.get('run')
        show_active = spec.get('show_active', True)
        if not isinstance(label, str) or any(v is not None and not isinstance(v, str) for v in (widget, run)):
            raise ConfigError(f'keys.{raw}: label, widget and run must be strings')
        if not isinstance(show_active, bool):
            raise ConfigError(f'keys.{raw}: show_active must be a boolean')
        if 'show_active' in spec and widget not in tiles.AGENT_WIDGETS:
            raise ConfigError(f'keys.{raw}: show_active requires an agent widget')
        if n == WINDOW_INDEX and (label or widget):
            raise ConfigError(f'keys.{raw} takes run but no label or widget')
        if label and widget:
            raise ConfigError(f'keys.{raw}: label and widget are exclusive')
        if widget is not None and widget not in tiles.WIDGET_NAMES:
            raise ConfigError(f'keys.{raw}: unknown widget {widget!r} (have {", ".join(tiles.WIDGET_NAMES)})')
        keys[n] = Key(label=label, widget=widget, run=run, show_active=show_active)
    return Config(brightness=brightness, heartbeat=heartbeat, refresh=refresh, clock=clock, keys=keys)


class DesiredState:
    """What every key should show; the daemon owns this, not the deck.

    Precedence per key: runtime override, then widget tile, then config label.
    """

    def __init__(self, config: Config):
        self.config = config
        self.overrides: dict[int, str] = {}
        self.widget_images: dict[int, bytes] = {}
        self.attention: dict[int, tuple] = {}

    def label(self, n: int) -> str:
        if n in self.overrides:
            return self.overrides[n]
        key = self.config.keys.get(n)
        return key.label if key else ''

    def visible_attention(self, n: int):
        attention = self.attention.get(n)
        key = self.config.keys.get(n)
        if attention and attention[1] == 'working' and key and not key.show_active:
            return None
        return attention

    def describe(self, n: int) -> str:
        key = self.config.keys.get(n)
        attention = self.visible_attention(n)
        if n not in self.overrides and attention:
            agent, state, count, repos, updated = attention
            return f'<{agent}:{state}:{count} {repos}>'
        if n not in self.overrides and key and key.widget:
            return f'<{key.widget}>'
        return repr(self.label(n))

    def image(self, n: int) -> bytes | None:
        key = self.config.keys.get(n)
        attention = self.visible_attention(n)
        if n not in self.overrides and attention:
            return tiles.render_attention(*attention)
        if n not in self.overrides and key and key.widget:
            return self.widget_images.get(n) or tiles.render_widget(
                key.widget, metrics.Sampler(), metrics.Sample(t=0))
        text = self.label(n)
        return deckzip.render_tile(text) if text else None

    def images(self) -> dict[int, bytes | None]:
        return {n: self.image(n) for n in range(KEY_COUNT)}


def changed_images(desired: dict[int, bytes | None], shown: dict[int, bytes | None] | None) -> dict:
    """Keys whose image differs from what the deck shows; everything if unknown."""
    if shown is None:
        return dict(desired)
    return {n: img for n, img in desired.items() if shown.get(n, None) != img or n not in shown}


class D200Device:
    def __init__(self, config_path: Path, socket_path: Path, *, name='default',
                 identity='unbound', config=None, profile='default'):
        self.config_path = config_path
        self.socket_path = socket_path
        self.name, self.identity, self.profile = name, identity, profile
        self.generation = 0
        self.descriptor = None
        self.present = False
        self.error = None
        self.task = None
        self.assigned_profile = profile
        self.state = DesiredState(config or load_config(config_path))
        self.sampler = metrics.Sampler()
        self.dev = None
        self.writer: deckproto.Writer | None = None
        self.shown: dict[int, bytes | None] | None = None  # None: deck contents unknown
        self.slots: dict[int, int] = {}  # which of a key's two icon names it shows
        self.running: dict[int, asyncio.Task] = {}
        self._push_pending = asyncio.Event()
        self._sync_lock = asyncio.Lock()
        self._lost: asyncio.Event | None = None
        self._sampler_failing = False
        self.last_sample: metrics.Sample | None = None
        self._tile_writes: list[float] = []
        self._tile_log_at = time.monotonic()

    def log(self, *args):
        log(f'device={self.name} identity={self.identity} generation={self.generation}', *args)

    @property
    def connected(self) -> bool:
        return self.writer is not None and not (self._lost and self._lost.is_set())

    # --- writes -------------------------------------------------------------

    async def write(self, packets) -> float | None:
        """Send through the serialized writer; a failed write drops the device."""
        writer = self.writer
        if writer is None:
            return None
        try:
            return await writer.send(packets)
        except OSError as e:
            self._lose(f'write failed: {e}')
            return None

    def _lose(self, why: str):
        if self._lost and not self._lost.is_set():
            self.log(f'device lost: {why}')
            self.error = why
            self._lost.set()

    async def sync(self, full: bool = False, quiet: bool = False) -> float | None:
        """Bring the keys to the desired state: a full upload if the deck's contents
        are unknown, otherwise one partial update holding only the changed keys."""
        async with self._sync_lock:
            if not self.connected:
                return
            writer, generation = self.writer, self.generation
            snapshot = copy.deepcopy(self.state)
            desired = await asyncio.to_thread(snapshot.images)
            full = full or self.shown is None
            changed = desired if full else changed_images(desired, self.shown)
            if not changed:
                return None
            slots = {n: 1 - self.slots.get(n, 1) for n in changed}
            names = {n: deckzip.slot_name(n, s) for n, s in slots.items()}
            blob = await asyncio.to_thread(deckzip.build_zip_images, changed, names=names)
            if writer is not self.writer or generation != self.generation or not self.connected:
                return None
            took = await self.write(deckproto.zip_frames(blob, partial=not full))
            if took is None or writer is not self.writer or generation != self.generation:
                return None
            self.slots.update(slots)
            if full:
                self.shown = dict(desired)
            else:
                self.shown.update(changed)
            if not quiet:
                keys = ','.join(key_name(n) for n in sorted(changed))
                self.log(f'{"full" if full else "partial"} upload keys={keys} bytes={len(blob)} write={took * 1000:.0f}ms')
            return took

    def request_push(self):
        self._push_pending.set()

    async def push_loop(self):
        while True:
            await self._push_pending.wait()
            await asyncio.sleep(PUSH_DEBOUNCE)
            self._push_pending.clear()
            await self.write(deckproto.brightness_frame(self.state.config.brightness))
            await self.sync()

    async def heartbeat_loop(self):
        """Liveness only: if nothing else wrote for `heartbeat` seconds, write something.
        Any write (clock, tiles, labels) resets the clock on this."""
        while True:
            await asyncio.sleep(0.25)
            w = self.writer
            if w and time.monotonic() - w.last_write >= self.state.config.heartbeat:
                # re-sending the current brightness holds the watchdog off with no
                # visible change (docs/qualification), and carries no clock semantics
                await self.write(deckproto.brightness_frame(self.state.config.brightness))

    async def clock_loop(self):
        """Visual only: keep the small window's clock current."""
        while True:
            interval = self.state.config.clock
            if interval <= 0:
                await asyncio.sleep(1)
                continue
            await asyncio.sleep(interval - (time.time() % interval) + 0.02)
            if self.connected:
                cur = self.last_sample
                await self.write(deckproto.clock_frame(cpu=cur and cur.cpu, mem=cur and cur.mem))

    async def tiles_loop(self):
        # CPU and network need two samples; take the first now so the first redraw has values
        try:
            self.last_sample = self.sampler.sample()
        except Exception:
            pass
        while True:
            await asyncio.sleep(self.state.config.refresh)
            widgets = self.state.config.widget_keys()
            try:
                cur = self.last_sample = self.sampler.sample()
                if not widgets and not self.state.attention:
                    continue
                config = self.state.config
                images = await asyncio.to_thread(lambda: {
                    n: tiles.render_widget(w, self.sampler, cur) for n, w in widgets.items()
                })
                if self.state.config is not config:
                    continue  # a reload already rendered the new configuration
                self.state.widget_images = images
                if self._sampler_failing:
                    self.log('metrics recovered')
                self._sampler_failing = False
            except Exception as e:
                # a broken sampler must not take buttons down with it
                if not self._sampler_failing:
                    self.log(f'metrics failed: {e!r}')
                self._sampler_failing = True
                continue
            took = await self.sync(quiet=True)
            if took is not None:
                self._tile_writes.append(took)
            if time.monotonic() - self._tile_log_at >= TILE_LOG_EVERY and self._tile_writes:
                w = sorted(self._tile_writes)
                self.log(f'tiles: {len(w)} uploads in {TILE_LOG_EVERY:.0f}s, write median '
                    f'{w[len(w) // 2] * 1000:.0f}ms max {w[-1] * 1000:.0f}ms')
                self._tile_writes, self._tile_log_at = [], time.monotonic()

    # --- commands -----------------------------------------------------------

    def press(self, n: int, source: str) -> str:
        if not 0 <= n <= WINDOW_INDEX:
            return 'invalid key'
        key = self.state.config.keys.get(n)
        self.log(f'press key={key_name(n)} raw={n} source={source}')
        if not key or not key.run:
            return 'no command'
        if n in self.running:
            self.log(f'key={key_name(n)} still running; press ignored')
            return 'already running'
        self.running[n] = asyncio.create_task(self.run_key(n, key.run))
        return 'started'

    def child_env(self, n: int) -> dict[str, str]:
        """Key commands can always call ulanzictl, and it reaches this daemon."""
        path = os.environ.get('PATH', '/usr/bin:/bin')
        return dict(os.environ, ULANZI_KEY=key_name(n), ULANZI_DEVICE=self.name,
                    ULANZI_IDENTITY=self.identity, ULANZI_SOCKET=str(self.socket_path),
                    PATH=f'{Path(sys.executable).parent}{os.pathsep}{REPO}{os.pathsep}{path}')

    async def run_key(self, n: int, command: str):
        env = self.child_env(n)
        proc = None
        try:
            starting = asyncio.create_task(asyncio.create_subprocess_shell(
                command, env=env, cwd=Path.home(), stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            ))
            try:
                proc = await asyncio.shield(starting)
            except asyncio.CancelledError:
                proc = await starting
                raise
            rc = await proc.wait()
            self.log(f'key={key_name(n)} exit={rc}')
        except Exception as e:
            self.log(f'key={key_name(n)} failed: {e}')
        finally:
            if proc is not None:
                # Key commands must not survive their daemon, including children
                # of the shell. A normal shell exit also retires its process group.
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                if proc.returncode is None:
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        pass
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
            self.running.pop(n, None)

    def apply_config(self, config):
        self.state.config = config
        self.state.widget_images = ({n: tiles.render_widget(w, self.sampler, self.last_sample)
                                     for n, w in config.widget_keys().items()}
                                    if self.last_sample is not None else {})
        self.request_push()

    def reload(self):
        self.apply_config(load_config(self.config_path))
        self.log('config reloaded')

    def handle(self, line: str) -> str:
        """One request line in, one reply line out: 'ok[ detail]' or 'err <why>'."""
        if line.startswith('target '):
            target_parts = line.split(maxsplit=2)
            if len(target_parts) != 3 or target_parts[1] not in (self.name, self.identity):
                return 'err unknown device'
            line = target_parts[2]
        parts = line.strip().split(maxsplit=2)
        if not parts:
            return 'err empty command'
        cmd, args = parts[0], parts[1:]
        try:
            if cmd == 'attention':
                fields = line.split(maxsplit=5)
                if len(fields) < 5:
                    raise ConfigError('attention needs key, agent, state, count and optional project names')
                _, raw_key, agent, state, raw_count, *names = fields
                n = parse_key(raw_key)
                if agent not in tiles.AGENTS or state not in ('waiting', 'done', 'error', 'interrupted', 'working'):
                    raise ConfigError('unknown attention agent or state')
                if not raw_count.isascii() or not raw_count.isdecimal() or len(raw_count) > 3 or not 1 <= int(raw_count) <= 256:
                    raise ConfigError('attention count must be 1..256')
                updated, projects = time.time(), names[0].strip() if names else ''
                if projects.startswith('@'):
                    stamp, _, projects = projects.partition(' ')
                    try:
                        updated = float(stamp[1:])
                    except ValueError:
                        raise ConfigError('invalid attention observation time') from None
                    if not math.isfinite(updated) or not 0 <= updated <= time.time() + 60:
                        raise ConfigError('invalid attention observation time')
                record = (agent, state, int(raw_count), projects[:160], updated)
                self.state.overrides.pop(n, None)
                self.state.attention[n] = record
                self.request_push()
                return 'ok'
            if cmd == 'label' and len(args) == 2:
                self.state.overrides[parse_key(args[0])] = args[1]
                self.request_push()
                return 'ok'
            if cmd == 'label' and len(args) == 1:
                # an explicit empty label blanks the key
                self.state.overrides[parse_key(args[0])] = ''
                self.request_push()
                return 'ok'
            if cmd == 'reset' and len(args) == 1:
                if args[0] == 'all':
                    self.state.overrides.clear()
                    self.state.attention.clear()
                else:
                    n = parse_key(args[0])
                    self.state.overrides.pop(n, None)
                    self.state.attention.pop(n, None)
                self.request_push()
                return 'ok'
            if cmd == 'press' and len(args) == 1:
                return 'ok ' + self.press(parse_key(args[0], window_ok=True), 'socket')
            if cmd == 'reload' and not args:
                self.reload()
                return 'ok'
            if cmd == 'status' and not args:
                keys = ' '.join(f'{key_name(n)}={self.state.describe(n)}' for n in range(KEY_COUNT))
                return f'ok connected={self.connected} {keys}'
        except ConfigError as e:
            return f'err {e}'
        return f'err unknown command: {line.strip()}'

    async def serve_client(self, reader, writer):
        try:
            line = await reader.readline()
            reply = self.handle(line.decode('utf-8', 'replace'))
            writer.write(reply.replace('\n', ' ').encode() + b'\n')
            await writer.drain()
        finally:
            writer.close()

    # --- device -------------------------------------------------------------

    async def restore_deck(self):
        """Everything the deck should show, sent after every (re)connect."""
        await self.write(deckproto.brightness_frame(self.state.config.brightness))
        await self.write(deckproto.clock_frame())
        self.shown = None
        await self.sync(full=True)

    async def session(self, descriptor, opener=deckproto.open_device):
        """One attachment only. Never reuses a transport after a failed session."""
        self.descriptor = descriptor
        workers = []
        dev = None
        try:
            opening = asyncio.create_task(asyncio.to_thread(opener, descriptor['path']))
            try:
                dev = await asyncio.shield(opening)
            except asyncio.CancelledError:
                results = await asyncio.gather(opening, return_exceptions=True)
                if not isinstance(results[0], BaseException):
                    dev = results[0]
                raise
            serial_reader = getattr(dev, 'get_serial_number_string', None)
            expected_serial = descriptor.get('serial')
            if expected_serial and serial_reader:
                checking = asyncio.create_task(asyncio.to_thread(serial_reader))
                try:
                    actual_serial = await asyncio.shield(checking)
                except asyncio.CancelledError:
                    await asyncio.gather(checking, return_exceptions=True)
                    raise
                if actual_serial != expected_serial:
                    raise OSError(f'opened serial {actual_serial!r} differs from enumerated '
                                  f'{expected_serial!r}; refusing reused HID path')
            self.generation += 1
            self.error = None
            self.dev, self.writer, self._lost = dev, deckproto.Writer(dev), asyncio.Event()
            self.shown = None  # keep physical icon alternation across handle reopen
            self.log('event=connected')
            workers = [asyncio.create_task(self.read_events(dev)),
                       asyncio.create_task(self.push_loop()),
                       asyncio.create_task(self.heartbeat_loop()),
                       asyncio.create_task(self.clock_loop()),
                       asyncio.create_task(self.tiles_loop())]
            await self.restore_deck()
            lost = asyncio.create_task(self._lost.wait())
            workers.append(lost)
            done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled() and task.exception():
                    raise task.exception()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self.error != str(e):
                self.log(f'event=session_failed error={e!r}')
            self.error = str(e)
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            # A thread write cannot be cancelled: drain it before closing the HID handle.
            writer = self.writer
            self.writer = self.dev = None
            self.shown = None
            if dev is not None:
                if writer:
                    async with writer.lock:
                        await asyncio.to_thread(dev.close)
                else:
                    await asyncio.to_thread(dev.close)
            if workers:
                self.log('event=disconnected')

    async def read_events(self, dev):
        while True:
            inp = dev.read(1024)  # non-blocking; raises OSError when the deck goes away
            if not inp:
                await asyncio.sleep(0.01)
                continue
            parsed = deckproto.parse_report(inp)
            if parsed and parsed[0] == 'button' and parsed[1][1]:
                self.press(parsed[1][0], 'deck')
            elif parsed and parsed[0] == 'info':
                self.log(f'event=device_info info={parsed[1]!r}')
            # Even a continuously readable/faulty unit must yield to healthy peers.
            await asyncio.sleep(0)


@dataclass
class DeviceRule:
    match: dict[str, str]
    profile: str


@dataclass
class FleetConfig:
    profiles: dict[str, Config]
    devices: dict[str, DeviceRule]


def load_fleet(path: Path) -> FleetConfig:
    try:
        data = tomllib.loads(path.read_text())
        if set(data) - {'deck', 'keys', 'profiles', 'devices'}:
            raise ConfigError('unknown top-level fields')
        base = parse_config(data)
        profiles = {'default': base}
        for name, spec in data.get('profiles', {}).items():
            if name == 'default':
                raise ConfigError('profile name default is reserved for the base configuration')
            if not isinstance(spec, dict) or set(spec) - {'deck', 'keys'}:
                raise ConfigError(f'profile {name}: expected deck/keys tables')
            profiles[name] = parse_config({
                'deck': dict(data.get('deck', {}), **spec.get('deck', {})),
                'keys': dict(data.get('keys', {}), **spec.get('keys', {})),
            })
        rules = {}
        for name, spec in data.get('devices', {}).items():
            if not name or any(c.isspace() for c in name) or name == 'all' or name.startswith('@'):
                raise ConfigError(f'invalid device name {name!r}')
            if not isinstance(spec, dict) or set(spec) - {'match', 'profile'}:
                raise ConfigError(f'device {name}: expected match/profile')
            match = spec.get('match', {})
            if (not isinstance(match, dict) or not match or
                    set(match) - {'serial', 'topology', 'path'} or
                    any(not isinstance(v, str) or not v for v in match.values())):
                raise ConfigError(f'device {name}: match needs serial, topology or path strings')
            profile = spec.get('profile', 'default')
            if profile not in profiles:
                raise ConfigError(f'device {name}: unknown profile {profile!r}')
            rules[name] = DeviceRule(match, profile)
        return FleetConfig(profiles, rules)
    except (OSError, tomllib.TOMLDecodeError, TypeError, AttributeError, ValueError) as e:
        raise ConfigError(f'{path}: {e}') from None


class DeviceRegistry:
    """Physical records persist across absence; sessions are attachment-scoped."""

    def __init__(self, daemon, enumerate_fn=deckproto.enumerate_devices,
                 opener=deckproto.open_device):
        self.daemon = daemon
        self.enumerate_fn, self.opener = enumerate_fn, opener
        self.devices: dict[str, D200Device] = {}
        self.discovery_error = None
        self.roles = {}
        self.colliding_serials: set[str] = set()
        self._reconcile_lock = asyncio.Lock()

    def assignments(self, descriptors, config):
        serial_counts = {}
        for d in descriptors:
            serial = d.get('serial', '')
            serial_counts[serial] = serial_counts.get(serial, 0) + 1
        self.colliding_serials.update(serial for serial, count in serial_counts.items()
                                      if serial and count > 1)
        result, names = {}, set()
        for raw in descriptors:
            d = dict(raw)
            serial = d.get('serial', '')
            if serial and serial not in self.colliding_serials:
                identity = 'serial:' + serial
            elif d.get('topology'):
                identity = 'port:' + d['topology']
            else:
                identity = 'path:' + os.fsdecode(d['path'])
            matches = [name for name, rule in config.devices.items()
                       if all((os.fsdecode(d['path']) if k == 'path' else d.get(k, '')) == v
                              for k, v in rule.match.items())]
            if serial in self.colliding_serials and any(
                    set(config.devices[name].match) == {'serial'} for name in matches):
                raise ConfigError(f'ambiguous serial {serial!r}; add topology or path to the role match')
            if len(matches) > 1:
                raise ConfigError(f'{identity}: matches multiple roles {matches}')
            name = matches[0] if matches else '@' + identity
            if name in names or identity in result:
                raise ConfigError(f'ambiguous identity/role {name}; add a topology match')
            names.add(name)
            profile = config.devices[name].profile if matches else 'default'
            result[identity] = (name, profile, d)
        return result

    async def reconcile(self, descriptors=None):
        # Serialize registry mutation, never transport or device update work.
        async with self._reconcile_lock:
            await self._reconcile(descriptors)

    async def _reconcile(self, descriptors=None):
        if descriptors is None:
            try:
                descriptors = await asyncio.to_thread(self.enumerate_fn)
            except Exception as e:
                self.discovery_error = str(e)
                log(f'event=discovery_failed error={e!r}')
                return  # failed enumeration is not proof of removal
        config = self.daemon.config
        try:
            assignments = self.assignments(descriptors, config)
        except ConfigError as e:
            self.discovery_error = str(e)
            log(f'event=discovery_ambiguous error={e!r}')
            return
        self.discovery_error = None
        for identity, device in self.devices.items():
            if identity.startswith('serial:') and identity[7:] in self.colliding_serials:
                device.name = '@' + identity
        # Signal all removals first. Draining one handle does not hold a peer lock.
        stops = []
        for identity, device in self.devices.items():
            item = assignments.get(identity)
            device.present = item is not None
            if device.task and (item is None or device.descriptor != item[2]):
                device.task.cancel()
                stops.append(device.task)
        if stops:
            await asyncio.gather(*stops, return_exceptions=True)
        if config is not self.daemon.config:
            # Reload may occur while an old write drains. Discard its old assignments.
            try:
                assignments = self.assignments(descriptors, self.daemon.config)
            except ConfigError as e:
                self.discovery_error = str(e)
                return
        for identity, (name, profile, descriptor) in assignments.items():
            device = self.devices.get(identity)
            if device is None:
                device = self.roles.pop(name, None)
                if device is None:
                    device = D200Device(self.daemon.config_path, self.daemon.socket_path,
                                        name=name, identity=identity, profile=profile,
                                        config=copy.deepcopy(self.daemon.config.profiles[profile]))
                device.identity = identity
                self.devices[identity] = device
            device.name = name
            device.present = True
            if (len(assignments) == 1 and not self.daemon.config.devices
                    and not self.daemon._legacy_adopted and len(self.devices) == 1):
                device.state.overrides.update(self.daemon.legacy.state.overrides)
                self.daemon._legacy_adopted = True
            if device.assigned_profile != profile:
                device.profile = profile
                device.assigned_profile = profile
            selected = device.profile
            if device.state.config != self.daemon.config.profiles[selected]:
                device.apply_config(copy.deepcopy(self.daemon.config.profiles[selected]))
            if device.task is None or device.task.done():
                device.descriptor = descriptor
                device.task = asyncio.create_task(device.session(descriptor, self.opener))

    async def run(self):
        try:
            while True:
                await self.reconcile()
                await asyncio.sleep(RECONNECT_DELAY)
        finally:
            tasks = [d.task for d in self.devices.values() if d.task]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class Daemon:
    def __init__(self, config_path: Path, socket_path: Path, *, enumerate_fn=None, opener=None):
        self.config_path, self.socket_path = config_path, socket_path
        self.config = load_fleet(config_path)
        self.registry = DeviceRegistry(self, enumerate_fn or deckproto.enumerate_devices,
                                       opener or deckproto.open_device)
        # Offline legacy control state is adopted only when exactly one physical unit exists.
        self.legacy = D200Device(config_path, socket_path, config=copy.deepcopy(self.config.profiles['default']))
        self._legacy_adopted = False
        self._reconcile_pending = asyncio.Event()

    def targets(self, target=None):
        devices = list(self.registry.devices.values())
        for name, rule in self.config.devices.items():
            if not any(d.name == name for d in devices):
                if name not in self.registry.roles:
                    self.registry.roles[name] = D200Device(
                        self.config_path, self.socket_path, name=name, identity='unbound:' + name,
                        profile=rule.profile, config=copy.deepcopy(self.config.profiles[rule.profile]))
                devices.append(self.registry.roles[name])
        if target == 'all':
            return devices
        if target and not devices and not self.config.devices and target in (self.legacy.name, self.legacy.identity):
            return [self.legacy]
        if target:
            found = [d for d in devices if target in (d.name, d.identity)]
            if len(found) != 1:
                raise ConfigError(f'unknown or ambiguous device {target!r}')
            return found
        if len(devices) == 1:
            device = devices[0]
            if not self._legacy_adopted and not self.config.devices:
                device.state.overrides.update(self.legacy.state.overrides)
                self._legacy_adopted = True
            return [device]
        if not devices and not self.config.devices:
            return [self.legacy]
        raise ConfigError('device target required; use target <name|identity|all> <command>')

    def inventory(self):
        rows = []
        for d in self.registry.devices.values():
            rows.append(dict(name=d.name, identity=d.identity, profile=d.profile,
                             state=('connected' if d.connected else 'absent' if not d.present
                                    else 'unhealthy' if d.error else 'connecting'),
                             generation=d.generation, error=d.error,
                             transport={k: os.fsdecode(v) if isinstance(v, bytes) else v
                                        for k, v in (d.descriptor or {}).items()},
                             running=[key_name(n) for n in d.running]))
        existing = {d.name for d in self.registry.devices.values()}
        for name, rule in self.config.devices.items():
            if name not in existing:
                pending = self.registry.roles.get(name)
                rows.append(dict(name=name, identity=None, profile=pending.profile if pending else rule.profile, state='absent',
                                 generation=0, match=rule.match))
        return dict(devices=rows, discovery_error=self.registry.discovery_error)

    def reload(self):
        config = load_fleet(self.config_path)
        self.registry.assignments([d.descriptor for d in self.registry.devices.values()
                                   if d.descriptor and d.task and not d.task.done()], config)
        for d in [*self.registry.devices.values(), *self.registry.roles.values()]:
            if d.name in config.devices:
                profile = config.devices[d.name].profile
            else:
                profile = 'default'
            if d.name in config.devices and d.descriptor:
                rule = config.devices[d.name]
                if not all((os.fsdecode(d.descriptor['path']) if k == 'path'
                            else d.descriptor.get(k, '')) == v for k, v in rule.match.items()):
                    d.name = '@' + d.identity
                    profile = 'default'
            elif d.name not in config.devices and d.identity != 'unbound':
                d.name = '@' + d.identity
            d.profile = d.assigned_profile = profile
            d.apply_config(copy.deepcopy(config.profiles[profile]))
        self.config = config
        self.legacy.state.config = copy.deepcopy(config.profiles['default'])
        self._reconcile_pending.set()
        log('event=config_reloaded')

    async def config_loop(self):
        while True:
            await self._reconcile_pending.wait()
            self._reconcile_pending.clear()
            await self.registry.reconcile()
            await asyncio.gather(*(d.write(deckproto.brightness_frame(d.state.config.brightness))
                                   for d in self.registry.devices.values() if d.connected))

    def handle(self, line):
        try:
            target = None
            if line.startswith('target '):
                parts = line.split(maxsplit=2)
                if len(parts) != 3:
                    raise ConfigError('target requires a device and command')
                _, target, line = parts
            if line.strip() in ('devices', 'inventory') or (line.strip() == 'status' and target is None
                                                          and len(self.registry.devices) != 1):
                return 'ok ' + json.dumps(self.inventory(), separators=(',', ':'))
            if line.strip() == 'reload':
                if target:
                    raise ConfigError('reload is host-wide')
                self.reload()
                return 'ok'
            selected = self.targets(target)
            if not selected:
                raise ConfigError('no devices to target')
            if line.startswith('profile '):
                profile = line[len('profile '):].strip()
                if profile not in self.config.profiles:
                    raise ConfigError(f'unknown profile {profile!r}')
                for d in selected:
                    d.profile = profile
                    d.apply_config(copy.deepcopy(self.config.profiles[profile]))
                return 'ok'
            # Validate before a broadcast mutates any desired state.
            parts = line.strip().split(maxsplit=2)
            if parts and parts[0] in ('label', 'press', 'reset') and len(parts) > 1:
                if not (parts[0] == 'reset' and parts[1] == 'all'):
                    parse_key(parts[1], window_ok=parts[0] == 'press')
            replies = [(d.name, d.handle(line)) for d in selected]
            if len(replies) == 1:
                return replies[0][1]
            errors = [r for _, r in replies if r.startswith('err ')]
            if errors:
                return errors[0]
            return 'ok ' + json.dumps(dict(replies), separators=(',', ':'))
        except ConfigError as e:
            return f'err {e}'

    async def serve_client(self, reader, writer):
        try:
            try:
                line = await reader.readline()
            except ValueError:
                reply = 'err request line too long'
            else:
                reply = self.handle(line.decode('utf-8', 'replace'))
            writer.write(reply.replace('\n', ' ').encode() + b'\n')
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def _reload_from_signal(self):
        try:
            self.reload()
        except ConfigError as e:
            log(f'event=reload_failed error={e!r}')

    async def run(self):
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Keep the lock inode: unlinking it at shutdown would let a third process
        # lock a different inode while a second process already owns the old one.
        lock = os.open(str(self.socket_path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ConfigError(f'daemon already owns {self.socket_path}') from None
            if os.path.lexists(self.socket_path):
                if not stat.S_ISSOCK(self.socket_path.lstat().st_mode):
                    raise ConfigError(f'refusing to replace non-socket {self.socket_path}')
                with socket_module.socket(socket_module.AF_UNIX) as probe:
                    probe.settimeout(1)
                    try:
                        probe.connect(str(self.socket_path))
                    except (ConnectionRefusedError, FileNotFoundError):
                        self.socket_path.unlink(missing_ok=True)
                    else:
                        raise ConfigError(f'daemon already listening on {self.socket_path}')
            await self._run_server()
        finally:
            os.close(lock)

    async def _run_server(self):
        server = await asyncio.start_unix_server(self.serve_client, path=str(self.socket_path))
        bound = self.socket_path.stat()
        os.chmod(self.socket_path, 0o600)
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGHUP, self._reload_from_signal)
        loop.add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
        log(f'listening on {self.socket_path}')
        workers = [asyncio.create_task(self.registry.run()), asyncio.create_task(self.config_loop())]
        try:
            async with server:
                await asyncio.gather(*workers)
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            # Commands remain daemon children across disconnects and finish on shutdown.
            commands = [t for d in [self.legacy, *self.registry.devices.values()]
                        for t in d.running.values()]
            for task in commands:
                task.cancel()
            await asyncio.gather(*commands, return_exceptions=True)
            loop.remove_signal_handler(signal.SIGHUP)
            loop.remove_signal_handler(signal.SIGTERM)
            try:
                current = self.socket_path.lstat()
                if (current.st_dev, current.st_ino) == (bound.st_dev, bound.st_ino):
                    self.socket_path.unlink()
            except FileNotFoundError:
                pass


def default_socket_path() -> Path:
    runtime = os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
    return Path(os.environ.get('ULANZI_SOCKET') or Path(runtime) / 'ulanzi.sock')


def user_config_path() -> Path:
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'ulanzi' / 'buttons.toml'


def default_config_path() -> Path:
    if os.environ.get('ULANZI_CONFIG'):
        return Path(os.environ['ULANZI_CONFIG']).expanduser()
    user = user_config_path()
    return user if user.exists() or not DEFAULT_CONFIG.exists() else DEFAULT_CONFIG


def bundled_config_path() -> Path:
    if DEFAULT_CONFIG.exists():
        return DEFAULT_CONFIG
    for entry in importlib.metadata.files('ulanzi') or ():
        if entry.as_posix().endswith('share/ulanzi/buttons.toml'):
            return Path(entry.locate())
    return Path(sys.prefix) / 'share/ulanzi/buttons.toml'


def main(argv=None):
    parser = argparse.ArgumentParser(description='Headless Linux Ulanzi D200 daemon')
    parser.add_argument('config_path', nargs='?', type=Path, help='legacy positional config path')
    parser.add_argument('-c', '--config', type=Path, help='buttons.toml path (default: user config, then source checkout)')
    parser.add_argument('--socket', type=Path, help='control socket (default: ULANZI_SOCKET or XDG_RUNTIME_DIR/ulanzi.sock)')
    parser.add_argument('--check', action='store_true', help='validate configuration and exit without opening a device or socket')
    parser.add_argument('--init-config', action='store_true', help='copy example config to the user config path; never overwrite')
    args = parser.parse_args(argv)
    if args.config and args.config_path:
        parser.error('choose --config or the positional config path, not both')
    config_path = args.config or args.config_path or default_config_path()

    async def start():
        await Daemon(config_path.expanduser().resolve(), args.socket or default_socket_path()).run()

    try:
        if args.init_config:
            target = args.config or args.config_path or Path(os.environ.get('ULANZI_CONFIG') or user_config_path())
            target = target.expanduser()
            sample = bundled_config_path()
            text = sample.read_text()
            load_fleet(sample)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with target.open('x') as output:
                    output.write(text)
            except FileExistsError:
                log(f'{target} already exists; not overwritten')
                return 1
            print(f'Created {target}; edit it, then run ulanzid --check')
            return 0
        if not config_path.expanduser().exists() and not (args.config or args.config_path or os.environ.get('ULANZI_CONFIG')):
            log(f'no config at {config_path}; run: ulanzid --init-config')
            return 1
        if args.check:
            config = load_fleet(config_path.expanduser())
            print(f'Configuration OK: {len(config.profiles)} profiles, {len(config.devices)} device roles')
            return 0
        asyncio.run(start())
    except (ConfigError, OSError) as e:
        log(f'bad config: {e}')
        return 1
    except KeyboardInterrupt:
        pass
    except asyncio.CancelledError:
        pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
