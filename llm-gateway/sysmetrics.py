"""Process and host metrics - what the load test cannot see from outside.

A load generator measures latency and throughput at the client. It cannot
tell you WHY throughput stopped climbing. That answer is on this side: which
process burned the CPU, whose resident memory grew, how long a ready task
waited before the event loop reached it.

Three things live here:

  SystemSampler   per-process and host counters, sampled on a timer
  LoopLagMonitor  event-loop scheduling delay, measured ON the loop
  start()         creates both and runs them as background tasks

Design rules, because observability that harms the thing it observes is
worse than none:

  1. psutil is OPTIONAL. If it is missing or a call is refused, the gateway
     serves traffic exactly as before and mg_sampler_available reads 0.
  2. Sampling runs in a worker thread, never on the event loop. Measuring
     loop lag from a task that itself blocks the loop would report the
     sampler's own cost as the service's latency.
  3. The sampler never raises into the application. Every failure lands in
     mg_sampler_errors_total with a stage label.
  4. CPU is exported as a COUNTER of seconds, not only as a percent gauge.
     A percent is already a derivative over a window the scraper did not
     choose; a counter lets Grafana pick its own window with rate().

Metric naming follows the Prometheus convention: base units (seconds,
bytes) and a _total suffix on counters.
"""

from __future__ import annotations

import asyncio
import gc
import os
import threading
import time
from typing import Any, Iterable, Sequence

try:  # psutil is a hard requirement for useful numbers, a soft one for boot
    import psutil
except Exception:  # pragma: no cover - exercised by test_sysmetrics
    psutil = None  # type: ignore[assignment]


# How often the sampler runs. 5s over a one-hour soak is 720 points, which
# is enough to see a memory trend and cheap enough to ignore.
SAMPLE_INTERVAL_S = float(os.environ.get("MG_SAMPLE_INTERVAL_S", "5"))

# How often the event loop is probed. Must be much finer than the sampler:
# lag is a sub-second phenomenon and a 5s probe would miss every spike.
LOOP_PROBE_INTERVAL_S = float(os.environ.get("MG_LOOP_PROBE_INTERVAL_S", "0.25"))

# Re-scan the process table this often. Processes restart; a cached handle
# to a dead PID reports a dead component forever.
DISCOVERY_INTERVAL_S = float(os.environ.get("MG_DISCOVERY_INTERVAL_S", "30"))

# Counting open sockets costs tens of milliseconds per process on Windows,
# which is more than everything else here put together. Off unless asked.
SAMPLE_CONNECTIONS = os.environ.get("MG_SAMPLE_CONNECTIONS", "0") == "1"

# The bucket ladder for event-loop lag lives in observability.py beside the
# request-latency one. Bucket edges are a contract a dashboard query reads,
# so they are declared in exactly one place.

# Lag recorded before this many seconds have passed belongs to starting up,
# not to serving. Building the pipeline and the first process-table scan
# land here and are an order of magnitude above steady state.
LOOP_WARMUP_S = float(os.environ.get("MG_LOOP_WARMUP_S", "20"))

# Probes used to measure the system timer's own overshoot before anything
# else runs. 20 at 250ms is five seconds, which is enough for a median and
# short enough not to delay the first real sample.
LOOP_CALIBRATION_PROBES = int(os.environ.get("MG_LOOP_CALIBRATION_PROBES", "20"))


# --------------------------------------------------------------------------
# Which OS processes make up which logical component
# --------------------------------------------------------------------------


class ProcessGroup:
    """One component, which may be several operating-system processes.

    Ollama is the reason this is a group rather than a process: the server
    starts a separate runner per loaded model, and the runner is the one
    holding the weights. Charging the parent alone would under-report the
    memory by gigabytes.
    """

    def __init__(
        self,
        name: str,
        *,
        self_process: bool = False,
        cmdline_contains: str | None = None,
        cmdline_name_hint: str | None = None,
        name_startswith: str | Sequence[str] | None = None,
        exe_contains: str | None = None,
        include_children: bool = False,
    ) -> None:
        self.name = name
        self.self_process = self_process
        self.cmdline_contains = (cmdline_contains or "").lower() or None
        # Only read a command line for processes whose NAME could plausibly
        # match, so Presidio is found by reading six command lines instead
        # of 266.
        #
        # Honest about the benefit: measured on this box, enumerating 266
        # processes by name alone costs ~17ms and adding every command line
        # takes it to ~24ms, so this saves under a millisecond. The cost is
        # in walking the table at all, not in the command lines. It is kept
        # because 260 avoided system calls is still the right shape and it
        # does not get worse on a busier machine - not because it is a
        # measurable win here.
        self.cmdline_name_hint = (cmdline_name_hint or "").lower() or None
        # A tuple, because a component's processes do not have to share a
        # name prefix. Ollama is the case that proved it: the server is
        # ollama.exe, but the thing holding the model weights and burning
        # the CPU is llama-server.exe. Matching only "ollama*" found 78MB
        # of supervisor and missed 3.7GB of model - so host CPU hit 100%
        # with nothing in the per-process breakdown to explain it.
        if name_startswith is None:
            prefixes: tuple[str, ...] = ()
        elif isinstance(name_startswith, str):
            prefixes = (name_startswith,)
        else:
            prefixes = tuple(name_startswith)
        self.name_startswith = tuple(p.lower() for p in prefixes if p) or None
        # Checked ONLY for processes whose name already matched, so this
        # costs a handful of calls rather than one per process. It keeps a
        # generic name like llama-server.exe from capturing someone else's
        # unrelated process.
        self.exe_contains = (exe_contains or "").lower() or None
        self.include_children = include_children
        self._procs: list[Any] = []
        # Previous per-process totals, keyed by process id:
        #   (sampled_at, cpu_user_s, cpu_system_s, io_read_b, io_write_b)
        # Kept so we can compute a true delta rather than rely on psutil's
        # stateful cpu_percent(), which silently returns 0.0 the first time
        # it is called on any given process object.
        self._last: dict[int, tuple[float, float, float, float, float]] = {}
        # Group totals we accumulate OURSELVES from those deltas.
        #
        # This is the whole reason the group exists. Summing the raw
        # per-process totals would make the group counter fall every time
        # Ollama retires a model runner, and a counter that falls is how
        # Prometheus is told a process restarted - so rate() would show a
        # phantom spike on every model unload. Accumulating only the
        # non-negative deltas gives a group total that is genuinely
        # monotonic across member churn.
        self._accum: dict[str, float] = {
            "cpu_user_s": 0.0,
            "cpu_system_s": 0.0,
            "io_read_bytes": 0.0,
            "io_write_bytes": 0.0,
        }

    # -- discovery ---------------------------------------------------------

    def _matches(self, proc: Any) -> bool:
        try:
            nm = (proc.info.get("name") if getattr(proc, "info", None)
                  else proc.name()) or ""
            nm = nm.lower()
        except Exception:
            return False
        try:
            if self.name_startswith and nm.startswith(self.name_startswith):
                if not self.exe_contains:
                    return True
                try:
                    return self.exe_contains in (proc.exe() or "").lower()
                except Exception:
                    # Cannot read the path - trust the name rather than
                    # drop a process we can see.
                    return True
            if self.cmdline_contains:
                # Skip the expensive read entirely for processes that could
                # not match anyway.
                if self.cmdline_name_hint and not nm.startswith(
                        self.cmdline_name_hint):
                    return False
                cl = " ".join(proc.cmdline() or []).lower()
                if self.cmdline_contains in cl:
                    return True
        except Exception:
            return False
        return False

    @property
    def needs_table_scan(self) -> bool:
        return bool(self.cmdline_contains or self.name_startswith)

    def discover(self, table: Sequence[Any] | None = None) -> None:
        """Refresh the process handles for this group.

        `table` is one already-taken snapshot of the process list, shared
        between groups. Walking the table costs a cmdline read per process
        and there is no reason to pay for it once per group.
        """
        if psutil is None:
            return
        found: list[Any] = []
        if self.self_process:
            try:
                me = psutil.Process(os.getpid())
                found.append(me)
                if self.include_children:
                    found.extend(me.children(recursive=True))
            except Exception:
                pass
        if self.needs_table_scan:
            if table is None:
                try:
                    table = list(psutil.process_iter(["name"]))
                except Exception:
                    table = []
            for proc in table:
                if self._matches(proc):
                    found.append(proc)
        # De-duplicate: a child of ours can also match a cmdline rule.
        seen: set[int] = set()
        unique: list[Any] = []
        for proc in found:
            try:
                if proc.pid in seen:
                    continue
                seen.add(proc.pid)
                unique.append(proc)
            except Exception:
                continue
        self._procs = unique
        # Drop history for process ids that no longer exist. Windows reuses
        # them aggressively, and a stale baseline against a recycled id
        # would produce a nonsense delta.
        live = {p.pid for p in unique}
        for pid in list(self._last):
            if pid not in live:
                self._last.pop(pid, None)

    # -- sampling ----------------------------------------------------------

    def sample(self, now: float) -> dict[str, Any] | None:
        """Aggregate this group. Returns None if nothing is running."""
        if psutil is None or not self._procs:
            return None

        agg: dict[str, Any] = {
            "count": 0,
            "rss": 0.0,
            "vms": 0.0,
            "threads": 0.0,
            "handles": 0.0,
            "cpu_user_s": 0.0,
            "cpu_system_s": 0.0,
            "cpu_percent": 0.0,
            "io_read_bytes": 0.0,
            "io_write_bytes": 0.0,
            "connections": 0.0,
            "oldest_start": None,
            "partial": 0,
        }

        for proc in list(self._procs):
            try:
                with proc.oneshot():
                    mem = proc.memory_info()
                    agg["rss"] += float(mem.rss)
                    agg["vms"] += float(getattr(mem, "vms", 0) or 0)

                    times = proc.cpu_times()
                    user = float(times.user)
                    system = float(times.system)

                    read_b = write_b = 0.0
                    try:
                        io = proc.io_counters()
                        read_b = float(getattr(io, "read_bytes", 0) or 0)
                        write_b = float(getattr(io, "write_bytes", 0) or 0)
                    except Exception:
                        agg["partial"] += 1

                    prev = self._last.get(proc.pid)
                    self._last[proc.pid] = (now, user, system, read_b, write_b)
                    if prev:
                        wall = now - prev[0]
                        d_user = user - prev[1]
                        d_system = system - prev[2]
                        # A negative delta means the process id was recycled
                        # under us. Contribute nothing rather than a spike.
                        if d_user >= 0:
                            self._accum["cpu_user_s"] += d_user
                        if d_system >= 0:
                            self._accum["cpu_system_s"] += d_system
                        if read_b >= prev[3]:
                            self._accum["io_read_bytes"] += read_b - prev[3]
                        if write_b >= prev[4]:
                            self._accum["io_write_bytes"] += write_b - prev[4]
                        burned = max(0.0, d_user) + max(0.0, d_system)
                        if wall > 0:
                            agg["cpu_percent"] += 100.0 * burned / wall
                    # A process seen for the FIRST time contributes nothing
                    # this round. Charging its whole historical total would
                    # put a step the size of the process's lifetime into the
                    # group counter the moment it is discovered.

                    try:
                        agg["threads"] += float(proc.num_threads())
                    except Exception:
                        agg["partial"] += 1

                    # num_fds on POSIX, num_handles on Windows. Both answer
                    # "is this process leaking kernel objects".
                    try:
                        if hasattr(proc, "num_fds"):
                            agg["handles"] += float(proc.num_fds())
                        elif hasattr(proc, "num_handles"):
                            agg["handles"] += float(proc.num_handles())
                    except Exception:
                        agg["partial"] += 1

                    if SAMPLE_CONNECTIONS:
                        try:
                            conns = (proc.net_connections(kind="inet")
                                     if hasattr(proc, "net_connections")
                                     else proc.connections(kind="inet"))
                            agg["connections"] += float(len(conns))
                        except Exception:
                            agg["partial"] += 1

                    try:
                        started = float(proc.create_time())
                        if (agg["oldest_start"] is None
                                or started < agg["oldest_start"]):
                            agg["oldest_start"] = started
                    except Exception:
                        agg["partial"] += 1

                agg["count"] += 1
            except Exception:
                # Vanished, or refused. Discovery will drop it next sweep.
                continue

        if not agg["count"]:
            return None
        # Group totals come from our own accumulators, never from the sum
        # of the current members' lifetime totals.
        agg["cpu_user_s"] = self._accum["cpu_user_s"]
        agg["cpu_system_s"] = self._accum["cpu_system_s"]
        agg["io_read_bytes"] = self._accum["io_read_bytes"]
        agg["io_write_bytes"] = self._accum["io_write_bytes"]
        return agg


def default_groups() -> list[ProcessGroup]:
    """The three components worth charging CPU and memory to."""
    return [
        ProcessGroup("gateway", self_process=True, include_children=True),
        ProcessGroup("presidio", cmdline_contains="presidio_service",
                     cmdline_name_hint="python"),
        # ollama.exe supervises; llama-server.exe holds the weights and
        # does the inference. Both, or the numbers are meaningless.
        ProcessGroup("ollama",
                     name_startswith=("ollama", "llama-server"),
                     exe_contains="ollama"),
    ]


# --------------------------------------------------------------------------
# The sampler
# --------------------------------------------------------------------------


class SystemSampler:
    """Per-process and host metrics, written into a Metrics registry."""

    def __init__(self, metrics: Any,
                 groups: Iterable[ProcessGroup] | None = None) -> None:
        self.metrics = metrics
        self.groups = list(groups) if groups is not None else default_groups()
        self._last_discovery = 0.0
        self._primed = False
        self.available = psutil is not None
        self.samples = 0

    # -- helpers -----------------------------------------------------------

    def _err(self, stage: str) -> None:
        try:
            self.metrics.inc("mg_sampler_errors_total", {"stage": stage})
        except Exception:
            pass

    def _discover_all(self, stage: str) -> None:
        """One process-table snapshot, shared by every group that needs it.

        Reading a command line is a per-process system call, so scanning
        the table once per component would multiply the cost of the single
        most expensive thing the sampler does.
        """
        started = time.perf_counter()
        table: list[Any] | None = None
        if psutil is not None and any(g.needs_table_scan for g in self.groups):
            try:
                # Names only. Command lines are read per candidate in
                # ProcessGroup._matches, for the handful that can match.
                table = list(psutil.process_iter(["name"]))
            except Exception:
                self._err(stage)
                table = []
        for group in self.groups:
            try:
                group.discover(table)
            except Exception:
                self._err(stage)
        self._last_discovery = time.time()
        try:
            self.metrics.set_gauge("mg_sampler_discovery_seconds",
                                   time.perf_counter() - started)
            self.metrics.set_gauge("mg_sampler_table_size",
                                   float(len(table) if table else 0))
        except Exception:
            pass

    def prime(self) -> None:
        """First call of a percent-style reading is always 0.0; burn it."""
        if psutil is None:
            self._primed = True
            return
        try:
            psutil.cpu_percent(interval=None)
            psutil.cpu_percent(interval=None, percpu=True)
        except Exception:
            self._err("prime")
        self._discover_all("prime_discover")
        for group in self.groups:
            try:
                group.sample(time.time())  # seeds the per-process baseline
            except Exception:
                self._err("prime_discover")
        self._primed = True

    # -- the sample --------------------------------------------------------

    def sample(self) -> None:
        """One full pass. Never raises."""
        started = time.perf_counter()
        m = self.metrics
        try:
            m.set_gauge("mg_sampler_available", 1 if self.available else 0)
            if psutil is None:
                return
            if not self._primed:
                self.prime()

            now = time.time()
            if now - self._last_discovery >= DISCOVERY_INTERVAL_S:
                self._discover_all("discover")

            self._sample_host()
            attributed = self._sample_processes(now)
            self._sample_python()
            self._sample_attribution(attributed)
            self.samples += 1
            m.set_gauge("mg_sampler_samples", float(self.samples))
        except Exception:
            self._err("sample")
        finally:
            # The sampler's own cost, so nobody has to wonder whether the
            # measurement is moving the thing it measures.
            try:
                m.set_gauge(
                    "mg_sampler_duration_seconds",
                    time.perf_counter() - started,
                )
            except Exception:
                pass

    def _sample_host(self) -> None:
        m = self.metrics
        try:
            m.set_gauge("mg_host_cpu_percent", float(psutil.cpu_percent(None)))
            for i, pct in enumerate(psutil.cpu_percent(None, percpu=True)):
                m.set_gauge("mg_host_cpu_percent_core", float(pct),
                            {"core": str(i)})
        except Exception:
            self._err("host_cpu")
        try:
            m.set_gauge("mg_host_cpu_count",
                        float(psutil.cpu_count(logical=False) or 0),
                        {"kind": "physical"})
            m.set_gauge("mg_host_cpu_count",
                        float(psutil.cpu_count(logical=True) or 0),
                        {"kind": "logical"})
        except Exception:
            self._err("host_cpu_count")
        try:
            vm = psutil.virtual_memory()
            m.set_gauge("mg_host_memory_total_bytes", float(vm.total))
            m.set_gauge("mg_host_memory_available_bytes", float(vm.available))
            m.set_gauge("mg_host_memory_used_percent", float(vm.percent))
        except Exception:
            self._err("host_memory")
        try:
            sw = psutil.swap_memory()
            # On a 20GB box running three model runtimes, the moment this
            # moves is the moment latency stops being about the model.
            m.set_gauge("mg_host_swap_used_bytes", float(sw.used))
            m.set_gauge("mg_host_swap_used_percent", float(sw.percent))
        except Exception:
            self._err("host_swap")

    def _sample_attribution(self, attributed: float) -> None:
        """How much of the machine's CPU this breakdown fails to explain.

        Added after a real miss. The Ollama group matched only ollama.exe,
        so during inference the host sat at 100% while the per-process
        breakdown accounted for about a quarter of it - and nothing said
        so. The missing 3.7GB and most of the CPU belonged to
        llama-server.exe, which did not match the name prefix.

        A breakdown that silently omits the biggest consumer is worse than
        no breakdown, because it invites a confident wrong conclusion. So
        the residual is published. If it is large, the component list is
        incomplete and the attribution should not be trusted.

        Noisy by construction: the host figure and the per-process figures
        are measured over slightly different windows, so small values and
        the occasional negative are expected. It is a smoke alarm, not a
        meter.
        """
        m = self.metrics
        try:
            host = m.get_gauge("mg_host_cpu_percent")
            if host is None:
                return
            m.set_gauge("mg_proc_cpu_attributed_percent_host", attributed)
            m.set_gauge("mg_host_cpu_unattributed_percent",
                        max(0.0, float(host) - attributed))
        except Exception:
            self._err("attribution")

    def _sample_processes(self, now: float) -> float:
        """Sample every group. Returns total CPU attributed, host percent."""
        m = self.metrics
        attributed = 0.0
        logical = 0
        try:
            logical = psutil.cpu_count(logical=True) or 0
        except Exception:
            pass

        for group in self.groups:
            label = {"proc": group.name}
            try:
                agg = group.sample(now)
            except Exception:
                self._err("proc_sample")
                agg = None

            if agg is None:
                # Still publish the series. A metric that disappears when
                # the process dies looks identical to a scrape failure.
                m.set_gauge("mg_proc_up", 0, label)
                m.set_gauge("mg_proc_count", 0, label)
                continue

            m.set_gauge("mg_proc_up", 1, label)
            m.set_gauge("mg_proc_count", float(agg["count"]), label)
            m.set_gauge("mg_proc_memory_rss_bytes", agg["rss"], label)
            m.set_gauge("mg_proc_memory_vms_bytes", agg["vms"], label)
            m.set_gauge("mg_proc_threads", agg["threads"], label)
            m.set_gauge("mg_proc_handles", agg["handles"], label)
            if SAMPLE_CONNECTIONS:
                m.set_gauge("mg_proc_connections", agg["connections"], label)
            if agg["oldest_start"] is not None:
                # A restart resets resident memory. Without this, a soak
                # cannot tell "no leak" from "it crashed and came back".
                m.set_gauge("mg_proc_start_time_seconds",
                            float(agg["oldest_start"]), label)
            m.set_gauge("mg_proc_partial_reads", float(agg["partial"]), label)

            # CPU as a counter of seconds - the shape Prometheus wants.
            m.set_counter("mg_proc_cpu_seconds_total", agg["cpu_user_s"],
                          dict(label, mode="user"))
            m.set_counter("mg_proc_cpu_seconds_total", agg["cpu_system_s"],
                          dict(label, mode="system"))
            m.set_counter("mg_proc_io_read_bytes_total", agg["io_read_bytes"],
                          label)
            m.set_counter("mg_proc_io_write_bytes_total", agg["io_write_bytes"],
                          label)

            # Percent of ONE core, which is the psutil and top convention
            # and can exceed 100 for a multi-threaded process...
            m.set_gauge("mg_proc_cpu_percent", agg["cpu_percent"], label)
            # ...and percent of the whole machine, which is the one that
            # answers "are we out of CPU".
            if logical:
                share = agg["cpu_percent"] / logical
                m.set_gauge("mg_proc_cpu_percent_host", share, label)
                attributed += share

        return attributed

    def _sample_python(self) -> None:
        """In-process runtime facts. Only ever true of the gateway."""
        m = self.metrics
        try:
            m.set_gauge("mg_py_threads", float(threading.active_count()))
        except Exception:
            self._err("py_threads")
        try:
            for gen, stat in enumerate(gc.get_stats()):
                m.set_counter("mg_py_gc_collections_total",
                              float(stat.get("collections", 0)),
                              {"generation": str(gen)})
                m.set_counter("mg_py_gc_uncollectable_total",
                              float(stat.get("uncollectable", 0)),
                              {"generation": str(gen)})
            for gen, value in enumerate(gc.get_count()):
                m.set_gauge("mg_py_gc_objects", float(value),
                            {"generation": str(gen)})
        except Exception:
            self._err("py_gc")


# --------------------------------------------------------------------------
# Event-loop lag
# --------------------------------------------------------------------------


class LoopLagMonitor:
    """How long a ready task waits before the event loop runs it.

    The probe sleeps for a known interval and measures how much longer than
    that it actually took. When a coroutine does blocking work - a
    synchronous file append, a CPU-bound regex, a DNS lookup - every other
    request waits, and that wait appears here and nowhere else.

    This is the metric to show instead of "threads active versus inactive":
    uvicorn serves on ONE event loop in ONE thread, so a thread count is
    flat by construction and says nothing about saturation.

    Two things make the raw number untrustworthy, and both are handled
    here rather than explained away afterwards.

    THE INSTRUMENT HAS A NOISE FLOOR. Windows runs a ~15.6ms system timer
    by default, so asyncio.sleep() overshoots by up to that much on a
    completely idle loop. Reporting 15ms of timer granularity as 15ms of
    event-loop lag would be reporting the instrument, not the system. So
    the floor is MEASURED at startup on a known-idle loop and published
    beside the readings: lag meaningfully above the floor is signal.

    STARTUP IS NOT STEADY STATE. Building the pipeline, sweeping provider
    health and the first process-table scan all land in the first seconds
    and produce lag an order of magnitude above anything that follows. A
    lifetime maximum would be pinned to that spike for the rest of the run
    and a one-hour soak would report it as its worst case. Startup is
    therefore timed separately and excluded from the histogram.
    """

    def __init__(self, metrics: Any,
                 interval_s: float = LOOP_PROBE_INTERVAL_S,
                 warmup_s: float = LOOP_WARMUP_S,
                 calibration_probes: int = LOOP_CALIBRATION_PROBES) -> None:
        self.metrics = metrics
        self.interval = max(0.01, float(interval_s))
        self.warmup_s = max(0.0, float(warmup_s))
        self.calibration_probes = max(0, int(calibration_probes))
        self.floor = 0.0
        self.max_lag = 0.0          # steady state only
        self.startup_max_lag = 0.0  # warm-up, reported separately
        self.probes = 0
        self.warm = False
        # Set once calibration is done, so the sampler does not start its
        # first process-table scan while we are measuring an idle loop.
        self.ready: asyncio.Event | None = None

    # -- recording ---------------------------------------------------------

    def probe_once(self, elapsed: float, warm: bool = True) -> float:
        """Record one measured interval. Returns the lag it implies.

        `warm` false means this probe belongs to startup: it updates the
        startup peak and nothing else.
        """
        lag = max(0.0, elapsed - self.interval)
        self.probes += 1
        try:
            self.metrics.set_gauge("mg_event_loop_lag_seconds_last", lag)
            if not warm:
                if lag > self.startup_max_lag:
                    self.startup_max_lag = lag
                self.metrics.set_gauge("mg_event_loop_lag_startup_max_seconds",
                                       self.startup_max_lag)
                return lag
            if lag > self.max_lag:
                self.max_lag = lag
            self.metrics.observe(lag, name="mg_event_loop_lag_seconds")
            self.metrics.set_gauge("mg_event_loop_lag_seconds_max",
                                   self.max_lag)
        except Exception:
            pass
        return lag

    # -- calibration -------------------------------------------------------

    async def calibrate(self) -> float:
        """Measure this machine's timer noise floor on an idle loop.

        The median overshoot is used rather than the minimum: the minimum
        flatters the instrument by picking its single luckiest tick.
        """
        loop = asyncio.get_running_loop()
        samples: list[float] = []
        for _ in range(self.calibration_probes):
            before = loop.time()
            await asyncio.sleep(self.interval)
            samples.append(max(0.0, loop.time() - before - self.interval))
        if samples:
            samples.sort()
            self.floor = samples[len(samples) // 2]
        try:
            self.metrics.set_gauge("mg_event_loop_lag_floor_seconds",
                                   self.floor)
        except Exception:
            pass
        return self.floor

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        if self.ready is None:
            self.ready = asyncio.Event()
        try:
            await self.calibrate()
        finally:
            self.ready.set()

        warm_at = loop.time() + self.warmup_s
        while True:
            before = loop.time()
            await asyncio.sleep(self.interval)
            after = loop.time()
            if not self.warm and after >= warm_at:
                self.warm = True
                try:
                    self.metrics.set_gauge("mg_event_loop_warm", 1)
                except Exception:
                    pass
            self.probe_once(after - before, warm=self.warm)
            try:
                self.metrics.set_gauge(
                    "mg_asyncio_tasks", float(len(asyncio.all_tasks(loop))))
            except Exception:
                pass


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


async def _sampler_loop(sampler: SystemSampler, interval_s: float,
                        wait_for: Any = None) -> None:
    """Run the sampler in a worker thread, forever.

    run_in_executor matters here. psutil walks the process table with
    blocking system calls; doing that on the event loop would add the
    sampler's own cost to mg_event_loop_lag_seconds and we would be
    measuring the thermometer. It is not a complete fix - the scan does
    enough Python-level work to hold the interpreter lock - which is the
    other reason the probe publishes a measured noise floor.

    `wait_for` holds the first scan back until the lag probe has finished
    calibrating, so the calibration really does see an idle loop.
    """
    loop = asyncio.get_running_loop()
    if wait_for is not None:
        try:
            await asyncio.wait_for(wait_for.wait(), timeout=30)
        except Exception:
            pass  # calibration failed or took too long; sample anyway
    try:
        await loop.run_in_executor(None, sampler.prime)
    except asyncio.CancelledError:
        raise
    except Exception:
        sampler._err("prime_loop")
    while True:
        await asyncio.sleep(interval_s)
        try:
            await loop.run_in_executor(None, sampler.sample)
        except asyncio.CancelledError:
            raise
        except Exception:
            sampler._err("loop")


def start(metrics: Any,
          *,
          interval_s: float = SAMPLE_INTERVAL_S,
          probe_interval_s: float = LOOP_PROBE_INTERVAL_S,
          groups: Iterable[ProcessGroup] | None = None
          ) -> tuple[SystemSampler, LoopLagMonitor, list[Any]]:
    """Create the sampler and the loop probe and start both as tasks."""
    sampler = SystemSampler(metrics, groups=groups)
    monitor = LoopLagMonitor(metrics, probe_interval_s)
    monitor.ready = asyncio.Event()
    tasks = [
        # Order matters: the probe calibrates against an idle loop, and the
        # sampler waits for it before its first process-table scan.
        asyncio.create_task(monitor.run()),
        asyncio.create_task(
            _sampler_loop(sampler, interval_s, wait_for=monitor.ready)),
    ]
    return sampler, monitor, tasks
