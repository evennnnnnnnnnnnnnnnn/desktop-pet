"""Watch every Orbh agent session Blacksmith knows about.

Blacksmith is a port registry, not a session API. It runs on a fixed loopback
port and lists the per-Flint `flint-server` processes it has spawned; each of
those mounts the actual Orbh API under `/orbh` and streams change events over
SSE. So discovery is two hops: ask Blacksmith for the servers, then talk to
each server.

`OrbhMonitor` runs entirely on background threads and reports through a
callback. Every failure is soft -- if Blacksmith is down, or a server dies, or
the schema shifts, the monitor reports an empty roster and keeps retrying. The
caller is expected to carry on as if the feature were switched off.
"""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

BLACKSMITH_ENDPOINT_FILE = Path.home() / ".nuucognition" / "blacksmith" / "blacksmith.json"
BLACKSMITH_FALLBACK = "http://127.0.0.1:13030"

# The session is blocked on a human. Note that "awaiting" is NOT this: an
# awaiting session is dormant but auto-wakeable, and paging the user for one
# would be a false alarm.
NEEDS_INPUT = "needs-input"

# The states Orbh itself counts as active. Terminal sessions vastly outnumber
# these -- a Flint can hold hundreds -- and none of them can ever want
# anything, so they never reach the pet. "awaiting" is excluded on purpose: it
# means dormant-but-auto-wakeable, which needs no human.
ACTIVE_STATES = ("working", NEEDS_INPUT)

DISCOVERY_INTERVAL = 30.0  # re-ask Blacksmith which servers exist
RESYNC_INTERVAL = 30.0  # safety net in case an SSE stream goes quiet without dying
RECONNECT_DELAY = 5.0  # after a stream drops, before rediscovering
COALESCE_DELAY = 0.25  # bunch up bursts of events into one refetch
HTTP_TIMEOUT = 10.0


class Session:
    """One agent session, flattened to what the pet needs to draw and launch."""

    def __init__(self, payload, flint_name, flint_path):
        self.id = payload.get("id") or ""
        self.title = payload.get("title") or payload.get("displayTitle") or self.id[:8]
        self.work_state = payload.get("workState") or "unknown"
        self.runtime = payload.get("runtime") or ""
        self.mode = payload.get("mode") or ""
        self.flint_name = flint_name
        self.flint_path = flint_path
        request = payload.get("pendingRequest") or {}
        self.question = request.get("question") or ""

    @property
    def needs_input(self):
        return self.work_state == NEEDS_INPUT

    @property
    def attachable(self):
        """Only interactive sessions have a terminal a human can take over."""
        return self.mode == "interactive"

    @property
    def label(self):
        return f"{self.flint_name} / {self.title}"


def _get_json(url):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def blacksmith_endpoint():
    """Where Blacksmith is listening, per the file it rewrites on every boot."""
    try:
        return json.loads(BLACKSMITH_ENDPOINT_FILE.read_text())["endpoint"]
    except (OSError, ValueError, KeyError):
        return BLACKSMITH_FALLBACK


def discover_servers():
    """The flint-servers Blacksmith currently has up, as (port, name, path)."""
    payload = _get_json(f"{blacksmith_endpoint()}/resources?kind=flint-server")
    servers = []
    for resource in payload.get("resources", []):
        port = (resource.get("ports") or {}).get("flint")
        if not port:
            continue
        meta = resource.get("meta") or {}
        path = resource.get("flintPath") or ""
        name = meta.get("name") or Path(path).name or f"port {port}"
        servers.append((int(port), name, path))
    return servers


def _iter_sse(stream):
    """Yield each complete SSE event from `stream` as a decoded data payload."""
    data_lines = []
    for raw in stream:
        line = raw.decode("utf-8", "replace").rstrip("\n").rstrip("\r")
        if line == "":  # blank line terminates an event
            if data_lines:
                try:
                    yield json.loads("\n".join(data_lines))
                except ValueError:
                    pass
                data_lines = []
            continue
        if line.startswith(":"):  # comment / keepalive
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())


class _ServerWatcher(threading.Thread):
    """Holds one SSE connection to one flint-server and refetches on change."""

    def __init__(self, port, flint_name, flint_path, publish, stop_event):
        super().__init__(daemon=True)
        self.port = port
        self.flint_name = flint_name
        self.flint_path = flint_path
        self.publish = publish
        self.stop_event = stop_event
        self.base = f"http://127.0.0.1:{port}"
        self._dirty = threading.Event()
        # Set when this watcher is finished, so its pump thread retires with
        # it. Without this, every reconnect would leave another pump behind.
        self._done = threading.Event()

    def fetch_sessions(self):
        """Every live session on this server.

        Asked for one state at a time because the endpoint takes a single
        `status`; that also keeps hundreds of finished sessions off the wire.
        """
        found = {}
        for state in ACTIVE_STATES:
            payload = _get_json(f"{self.base}/orbh/sessions?limit=200&status={state}")
            for item in payload.get("sessions", []):
                # A status the server does not recognise is ignored rather than
                # rejected, and it answers with every session it has. Re-check
                # the state here so that can never flood the pet.
                if item.get("id") and item.get("workState") in ACTIVE_STATES:
                    found[item["id"]] = Session(item, self.flint_name, self.flint_path)
        return list(found.values())

    def _refetch(self):
        try:
            self.publish(self.port, self.fetch_sessions())
        except (urllib.error.URLError, OSError, ValueError):
            pass  # transient; the next event or the resync tick will retry

    def _pump(self):
        """Refetch whenever the stream says something changed.

        The events carry full session objects, but refetching the list is far
        less code than replicating Orbh's merge-and-evict rules, and the events
        are infrequent enough that the extra request costs nothing.
        """
        while not (self.stop_event.is_set() or self._done.is_set()):
            if self._dirty.wait(timeout=RESYNC_INTERVAL):
                self._dirty.clear()
                self.stop_event.wait(COALESCE_DELAY)  # let a burst settle
            if self.stop_event.is_set() or self._done.is_set():
                return
            self._refetch()

    def run(self):
        pump = threading.Thread(target=self._pump, daemon=True)
        pump.start()
        try:
            self._refetch()  # don't wait for the first event to show something
            url = f"{self.base}/events/stream?channels=orbh"
            request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
            with urllib.request.urlopen(request, timeout=None) as stream:
                for _event in _iter_sse(stream):
                    if self.stop_event.is_set():
                        return
                    self._dirty.set()
        except (urllib.error.URLError, OSError, ValueError):
            pass  # the supervisor rediscovers and respawns us
        finally:
            self._done.set()
            self._dirty.set()  # wake the pump so it can notice and retire


class OrbhMonitor:
    """Aggregates sessions across every Flint that Blacksmith has a server for.

    `on_update(sessions)` is invoked from a background thread whenever the
    roster changes -- callers driving a UI toolkit must marshal it themselves.
    """

    def __init__(self, on_update):
        self.on_update = on_update
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._by_port = {}  # port -> [Session]
        self._watchers = {}  # port -> _ServerWatcher
        self._last_signature = None

    def start(self):
        threading.Thread(target=self._supervise, daemon=True).start()

    def stop(self):
        self._stop.set()

    def sessions(self):
        """Every known session, needs-input first, then by label."""
        with self._lock:
            everything = [s for group in self._by_port.values() for s in group]
        everything.sort(key=lambda s: (not s.needs_input, s.label.lower()))
        return everything

    def needs_input(self):
        return [s for s in self.sessions() if s.needs_input]

    def _publish(self, port, sessions):
        with self._lock:
            self._by_port[port] = sessions
        self._emit()

    def _emit(self):
        """Notify the caller, but only when something actually changed."""
        current = self.sessions()
        signature = tuple((s.id, s.work_state, s.label) for s in current)
        if signature == self._last_signature:
            return
        self._last_signature = signature
        try:
            self.on_update(current)
        except Exception:  # a broken callback must not kill the monitor
            pass

    def _supervise(self):
        """Keep one watcher per live server; ports are ephemeral, so recheck."""
        while not self._stop.is_set():
            try:
                servers = discover_servers()
            except (urllib.error.URLError, OSError, ValueError):
                servers = []  # Blacksmith down: report nothing, try again shortly

            live = {port for port, _name, _path in servers}
            for port in list(self._watchers):
                if not self._watchers[port].is_alive():
                    # Its stream dropped. Drop the watcher but keep its last
                    # known sessions, so a reconnect doesn't blink the roster
                    # empty and send the pet home and straight back again.
                    self._watchers.pop(port)
                if port not in live:
                    self._watchers.pop(port, None)
                    with self._lock:
                        self._by_port.pop(port, None)
            self._emit()

            for port, name, path in servers:
                if port not in self._watchers:
                    watcher = _ServerWatcher(port, name, path, self._publish, self._stop)
                    self._watchers[port] = watcher
                    watcher.start()

            delay = DISCOVERY_INTERVAL if servers else RECONNECT_DELAY
            self._stop.wait(delay)
