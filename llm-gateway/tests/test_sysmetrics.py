"""Tests for process, host and event-loop metrics.

Two things are being proved here, and the second matters more.

First, that the numbers are right: the buckets, the quantile estimate, the
monotonic counter, the exposition format.

Second, that the telemetry CANNOT take the gateway down. A sampler walks the
process table, touches processes it does not own and reads counters the
operating system may refuse. Every one of those is a failure mode, and a
monitoring component that crashes the thing it monitors is worse than no
monitoring at all. Most of the tests below are about the second point.
"""

from __future__ import annotations

import asyncio
import re

import pytest

import sysmetrics
from observability import (
    HISTOGRAM_BUCKETS,
    LATENCY_BUCKETS,
    LOOP_LAG_BUCKETS,
    Metrics,
    _quantile,
)


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------


@pytest.mark.performance
def test_each_histogram_uses_its_own_bucket_ladder():
    """
    GIVEN request latency measured in seconds and loop lag in milliseconds
    WHEN  both are recorded
    THEN  each lands on its own ladder

    Sharing one ladder would put every healthy lag sample in the first
    bucket, so the histogram would be blind in exactly the range where a
    blocked event loop shows itself.
    """
    m = Metrics()
    m.observe(3.0)                                      # request latency
    m.observe(0.004, name="mg_event_loop_lag_seconds")  # loop lag

    body = m.render_prometheus()

    assert 'mg_request_duration_seconds_bucket{le="5.0"} 1' in body
    assert 'mg_event_loop_lag_seconds_bucket{le="0.005"} 1' in body
    # The ladders really are different, not a copy.
    assert HISTOGRAM_BUCKETS["mg_event_loop_lag_seconds"] == LOOP_LAG_BUCKETS
    assert HISTOGRAM_BUCKETS["mg_request_duration_seconds"] == LATENCY_BUCKETS


@pytest.mark.performance
def test_a_counter_never_goes_backwards():
    """
    GIVEN CPU seconds read from the operating system
    WHEN  a later read returns a LOWER value
    THEN  the lower value is refused

    Prometheus reads any decrease in a counter as a process restart and
    resets its rate calculation. A recycled process identifier or a partial
    read would otherwise show up in Grafana as a phantom CPU spike.
    """
    m = Metrics()
    label = {"proc": "gateway", "mode": "user"}

    m.set_counter("mg_proc_cpu_seconds_total", 12.5, label)
    m.set_counter("mg_proc_cpu_seconds_total", 9.0, label)

    assert m.get_counter("mg_proc_cpu_seconds_total", label) == 12.5

    m.set_counter("mg_proc_cpu_seconds_total", 20.0, label)
    assert m.get_counter("mg_proc_cpu_seconds_total", label) == 20.0


@pytest.mark.performance
def test_quantile_interpolates_inside_the_bucket_it_lands_in():
    """
    GIVEN a known distribution across known buckets
    WHEN  a quantile is estimated
    THEN  it falls inside the right bucket, interpolated

    A histogram does not keep samples, so this can never be exact. What it
    must be is BOUNDED - never outside the bucket the quantile belongs to.
    """
    edges = (1.0, 2.0, 3.0)
    buckets = [10, 10, 10, 0]  # 10 in each bucket, none past the last edge

    assert _quantile(buckets, edges, 0.50) == pytest.approx(1.5, abs=0.01)
    p90 = _quantile(buckets, edges, 0.90)
    assert 2.0 < p90 <= 3.0


@pytest.mark.performance
def test_quantile_in_the_overflow_bucket_reports_the_last_edge_as_a_floor():
    """
    GIVEN samples beyond the last bucket edge
    WHEN  p99 is estimated
    THEN  the last edge is returned

    It cannot be bounded above - that is what +Inf means. Reporting the
    edge is the honest answer and matches what Prometheus does; reporting
    an interpolated number would be inventing precision.
    """
    edges = (1.0, 2.0)
    buckets = [1, 1, 98]  # 98 samples larger than 2.0

    assert _quantile(buckets, edges, 0.99) == 2.0


@pytest.mark.performance
def test_quantile_of_nothing_is_none_not_zero():
    """An empty histogram has no p99. Zero would be a lie that reads well."""
    assert _quantile([0, 0, 0], (1.0, 2.0), 0.99) is None
    assert Metrics().histogram("mg_event_loop_lag_seconds") is None


@pytest.mark.performance
def test_exposition_output_is_parseable_prometheus():
    """
    GIVEN every kind of metric this gateway emits
    WHEN  /metrics is rendered
    THEN  every line parses as name{labels} value

    A malformed line does not raise anywhere - Prometheus just drops the
    scrape. This is the only place that failure is visible.
    """
    m = Metrics()
    m.inc("mg_requests_total", {"outcome": "ok"})
    m.set_gauge("mg_proc_memory_rss_bytes", 123456.0, {"proc": "gateway"})
    m.set_counter("mg_proc_cpu_seconds_total", 1.5,
                  {"proc": "ollama", "mode": "system"})
    m.observe(2.0, {"outcome": "ok"})
    m.observe(0.01, name="mg_event_loop_lag_seconds")

    line_re = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[^}]*\})? -?[0-9.eE+]+$')
    for line in m.render_prometheus().splitlines():
        if not line or line.startswith("#"):
            continue
        assert line_re.match(line), f"unparseable exposition line: {line!r}"


# --------------------------------------------------------------------------
# The sampler must never break the gateway
# --------------------------------------------------------------------------


class _ExplodingMetrics:
    """A registry where every write fails."""

    def inc(self, *a, **k):
        raise RuntimeError("registry down")

    def set_gauge(self, *a, **k):
        raise RuntimeError("registry down")

    def set_counter(self, *a, **k):
        raise RuntimeError("registry down")

    def observe(self, *a, **k):
        raise RuntimeError("registry down")


class _HostileGroup(sysmetrics.ProcessGroup):
    """A process group whose every operation raises, the way a vanished or
    access-denied process does on Windows."""

    def discover(self):
        raise OSError("access denied")

    def sample(self, now):
        raise OSError("process vanished")


@pytest.mark.performance
def test_sampler_runs_with_psutil_missing(monkeypatch):
    """
    GIVEN psutil is not installed
    WHEN  the sampler runs
    THEN  it reports itself unavailable and does not raise

    psutil is a convenience, not a dependency of serving traffic. A missing
    optional library must never stop the gateway booting.
    """
    monkeypatch.setattr(sysmetrics, "psutil", None)
    m = Metrics()
    sampler = sysmetrics.SystemSampler(m, groups=[])

    sampler.prime()
    sampler.sample()

    assert sampler.available is False
    assert m.get_gauge("mg_sampler_available") == 0


@pytest.mark.performance
def test_sampler_survives_a_process_that_refuses_every_call():
    """
    GIVEN a process that raises on discovery and on sampling
    WHEN  the sampler runs
    THEN  it records the error and keeps going

    This is the common case on Windows, not an exotic one: a process owned
    by another user, or one that exits between discovery and sampling.
    """
    m = Metrics()
    sampler = sysmetrics.SystemSampler(m, groups=[_HostileGroup("hostile")])

    sampler.prime()
    sampler.sample()  # must not raise

    assert m.get_counter("mg_sampler_errors_total",
                         {"stage": "prime_discover"}) >= 1
    # And the component is reported DOWN rather than silently absent.
    assert m.get_gauge("mg_proc_up", {"proc": "hostile"}) == 0


@pytest.mark.performance
def test_sampler_survives_a_registry_that_throws():
    """The sampler swallows failures from the thing it writes into, too."""
    sampler = sysmetrics.SystemSampler(_ExplodingMetrics(), groups=[])
    sampler.sample()  # the assertion is that this returns at all


@pytest.mark.performance
def test_a_dead_component_reads_zero_rather_than_disappearing():
    """
    GIVEN a component that is not running
    WHEN  metrics are rendered
    THEN  mg_proc_up is present and 0

    A series that vanishes looks exactly like a failed scrape. Explicitly
    publishing zero is the difference between "Presidio is down" and "we
    have no idea".
    """
    m = Metrics()
    group = sysmetrics.ProcessGroup("nothing", cmdline_contains="n0_such_proc")
    sampler = sysmetrics.SystemSampler(m, groups=[group])

    sampler.prime()
    sampler.sample()

    assert m.get_gauge("mg_proc_up", {"proc": "nothing"}) == 0
    assert 'mg_proc_up{proc="nothing"} 0' in m.render_prometheus()


# --------------------------------------------------------------------------
# Aggregating a group whose members come and go
# --------------------------------------------------------------------------


class _FakeProc:
    """A process we can drive, so member churn can be tested deterministically.

    Service virtualization. Ollama retiring a model runner mid-soak is the
    behaviour under test, and waiting for the real thing to do it is not a
    test, it is a hope.
    """

    def __init__(self, pid, cpu_user=0.0, cpu_system=0.0, rss=1000.0,
                 read_b=0.0, write_b=0.0):
        self.pid = pid
        self.cpu_user = cpu_user
        self.cpu_system = cpu_system
        self.rss = rss
        self.read_b = read_b
        self.write_b = write_b

    class _Null:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False

    def oneshot(self):
        return self._Null()

    def memory_info(self):
        return type("M", (), {"rss": self.rss, "vms": self.rss * 2})()

    def cpu_times(self):
        return type("T", (), {"user": self.cpu_user,
                              "system": self.cpu_system})()

    def io_counters(self):
        return type("I", (), {"read_bytes": self.read_b,
                              "write_bytes": self.write_b})()

    def num_threads(self):
        return 4

    def num_handles(self):
        return 20

    def create_time(self):
        return 1000.0


def _group_with(procs):
    group = sysmetrics.ProcessGroup("fake")
    group._procs = list(procs)
    return group


@pytest.mark.performance
def test_group_cpu_counter_does_not_fall_when_a_member_exits():
    """
    GIVEN two processes in a group, then one of them exits
    WHEN  the group's CPU counter is read
    THEN  it keeps climbing

    This is the Ollama case. Summing the members' lifetime totals would
    make the group counter DROP by the departed runner's whole lifetime,
    and Prometheus reads a falling counter as a restart - so rate() would
    report a phantom spike on every model unload. The group accumulates
    non-negative deltas instead.
    """
    a = _FakeProc(1, cpu_user=10.0)
    b = _FakeProc(2, cpu_user=5.0)
    group = _group_with([a, b])

    group.sample(100.0)                     # first sight: baseline only
    a.cpu_user, b.cpu_user = 12.0, 7.0
    second = group.sample(105.0)
    assert second["cpu_user_s"] == pytest.approx(4.0)   # 2 + 2

    # The big runner exits and is dropped from the group.
    group._procs = [a]
    a.cpu_user = 14.0
    third = group.sample(110.0)

    assert third["cpu_user_s"] == pytest.approx(6.0), (
        "group CPU fell when a member exited - Prometheus will read that "
        "as a counter reset"
    )
    assert third["cpu_user_s"] >= second["cpu_user_s"]


@pytest.mark.performance
def test_a_newly_discovered_process_does_not_add_its_whole_history():
    """
    GIVEN a long-running process discovered for the first time
    WHEN  it joins the group
    THEN  the group counter does not jump by its lifetime total

    Ollama may have been running for hours before the gateway started.
    Charging those hours at the moment of discovery would put a step into
    the graph that looks exactly like a burst of load.
    """
    a = _FakeProc(1, cpu_user=1.0)
    group = _group_with([a])
    group.sample(100.0)
    a.cpu_user = 2.0
    group.sample(105.0)

    veteran = _FakeProc(9, cpu_user=9999.0)  # running since this morning
    group._procs = [a, veteran]
    after = group.sample(110.0)

    assert after["cpu_user_s"] < 10.0, (
        f"discovery added {after['cpu_user_s']:.0f}s of history as if it "
        "had just been burned"
    )


@pytest.mark.performance
def test_recycled_process_id_does_not_produce_a_negative_delta():
    """
    GIVEN a process id reused by a different, younger process
    WHEN  its CPU total comes back LOWER than the baseline
    THEN  the drop contributes nothing

    Windows reuses process ids aggressively. Without this, a reused id
    subtracts from the group total.
    """
    a = _FakeProc(1, cpu_user=100.0)
    group = _group_with([a])
    group.sample(100.0)

    a.cpu_user = 3.0  # same id, brand new process
    after = group.sample(105.0)

    assert after["cpu_user_s"] == 0.0
    assert after["cpu_percent"] >= 0.0


@pytest.mark.performance
def test_the_ollama_group_includes_the_process_holding_the_model():
    """
    GIVEN the default component list
    WHEN  the Ollama group's matching rules are read
    THEN  llama-server is among them

    A REGRESSION TEST FOR A REAL MISS. Matching only "ollama*" found the
    supervisor at 78MB and missed llama-server.exe holding 3.7GB and doing
    all the inference. The load test then showed host CPU at 100% with a
    per-process breakdown that explained about a quarter of it.
    """
    ollama = {g.name: g for g in sysmetrics.default_groups()}["ollama"]

    assert ollama.name_startswith is not None
    assert "llama-server" in ollama.name_startswith
    assert "ollama" in ollama.name_startswith
    # Scoped to the Ollama installation, so an unrelated llama-server
    # belonging to someone else is not charged to us.
    assert ollama.exe_contains == "ollama"


@pytest.mark.performance
def test_cpu_the_breakdown_cannot_explain_is_published():
    """
    GIVEN a host at 80% with only 10% attributed to known components
    WHEN  the sampler finishes a pass
    THEN  the 70% it cannot explain is published

    This is the alarm for the bug above. A breakdown that silently omits
    the biggest consumer invites a confident wrong conclusion, so the
    residual is a metric rather than something a reader has to work out.
    """
    m = Metrics()
    sampler = sysmetrics.SystemSampler(m, groups=[])
    m.set_gauge("mg_host_cpu_percent", 80.0)

    sampler._sample_attribution(10.0)

    assert m.get_gauge("mg_proc_cpu_attributed_percent_host") == 10.0
    assert m.get_gauge("mg_host_cpu_unattributed_percent") == 70.0


@pytest.mark.performance
def test_unattributed_cpu_never_reports_negative():
    """Host and per-process figures use slightly different windows, so the
    difference can go negative. Negative CPU is not a thing; clamp it."""
    m = Metrics()
    sampler = sysmetrics.SystemSampler(m, groups=[])
    m.set_gauge("mg_host_cpu_percent", 20.0)

    sampler._sample_attribution(25.0)

    assert m.get_gauge("mg_host_cpu_unattributed_percent") == 0.0


@pytest.mark.performance
def test_the_process_table_is_scanned_once_for_all_groups(monkeypatch):
    """
    GIVEN three components, two of which are found by scanning
    WHEN  discovery runs
    THEN  the process table is walked exactly once

    Reading a command line is a system call per process. Scanning once per
    component would make the sampler's own cost scale with how many things
    it watches, which is the wrong direction.
    """
    pytest.importorskip("psutil")
    calls = {"n": 0}
    real_iter = sysmetrics.psutil.process_iter

    def counting_iter(*a, **k):
        calls["n"] += 1
        return real_iter(*a, **k)

    monkeypatch.setattr(sysmetrics.psutil, "process_iter", counting_iter)

    sampler = sysmetrics.SystemSampler(Metrics(),
                                       groups=sysmetrics.default_groups())
    sampler._discover_all("test")

    assert calls["n"] == 1, f"process table scanned {calls['n']} times"


# --------------------------------------------------------------------------
# What the sampler actually measures
# --------------------------------------------------------------------------


@pytest.mark.performance
def test_sampler_measures_this_very_process():
    """
    GIVEN the test process itself as the gateway group
    WHEN  the sampler runs twice
    THEN  resident memory, threads and CPU seconds are all present

    Running it twice matters: CPU is a delta between samples, so a single
    sample can only establish a baseline.
    """
    psutil = pytest.importorskip("psutil")
    m = Metrics()
    group = sysmetrics.ProcessGroup("gateway", self_process=True)
    sampler = sysmetrics.SystemSampler(m, groups=[group])

    sampler.prime()
    sampler.sample()

    label = {"proc": "gateway"}
    assert m.get_gauge("mg_proc_up", label) == 1
    assert m.get_gauge("mg_proc_memory_rss_bytes", label) > 0
    assert m.get_gauge("mg_proc_threads", label) >= 1
    assert m.get_counter("mg_proc_cpu_seconds_total",
                         dict(label, mode="user")) >= 0
    # Host facts come from the same pass.
    assert m.get_gauge("mg_host_cpu_count", {"kind": "logical"}) >= 1
    assert m.get_gauge("mg_host_memory_total_bytes") > 0


@pytest.mark.performance
def test_sampler_reports_its_own_cost():
    """
    GIVEN the sampler has run
    WHEN  its duration is read
    THEN  it is recorded

    A monitor that does not measure itself cannot answer the first question
    anyone asks of a performance number: how much did measuring it cost?
    """
    pytest.importorskip("psutil")
    m = Metrics()
    sampler = sysmetrics.SystemSampler(m, groups=sysmetrics.default_groups())

    sampler.prime()
    sampler.sample()

    cost = m.get_gauge("mg_sampler_duration_seconds")
    assert cost is not None and cost >= 0
    # If sampling costs more than a request, the budget is wrong.
    assert cost < 1.0, f"sampler took {cost:.3f}s, too expensive to run inline"


# --------------------------------------------------------------------------
# Event-loop lag
# --------------------------------------------------------------------------


@pytest.mark.performance
def test_loop_lag_is_the_excess_over_the_requested_sleep():
    """
    GIVEN a probe that asked to sleep 250ms and actually took 400ms
    WHEN  the lag is recorded
    THEN  it is 150ms, not 400ms

    Lag is the DELAY, not the duration. Reporting the whole interval would
    make a healthy loop look permanently saturated.
    """
    m = Metrics()
    monitor = sysmetrics.LoopLagMonitor(m, interval_s=0.25)

    assert monitor.probe_once(0.40) == pytest.approx(0.15, abs=1e-6)
    assert m.get_gauge("mg_event_loop_lag_seconds_last") == pytest.approx(0.15)


@pytest.mark.performance
def test_startup_lag_is_kept_out_of_the_steady_state_peak():
    """
    GIVEN a large lag spike during warm-up and small ones after
    WHEN  the steady-state maximum is read
    THEN  the startup spike is not in it, and is reported separately

    Building the pipeline and the first process-table scan produce
    hundreds of milliseconds of lag in the first seconds. A lifetime
    maximum would be pinned to that for the rest of the run, so a
    one-hour soak would report its worst case as something that happened
    before it served a single request.
    """
    m = Metrics()
    monitor = sysmetrics.LoopLagMonitor(m, interval_s=0.25)

    monitor.probe_once(0.25 + 0.52, warm=False)   # startup spike
    monitor.probe_once(0.25 + 0.02, warm=True)    # steady state
    monitor.probe_once(0.25 + 0.03, warm=True)

    assert monitor.startup_max_lag == pytest.approx(0.52, abs=1e-6)
    assert monitor.max_lag == pytest.approx(0.03, abs=1e-6)
    # And the histogram the percentiles come from excludes startup too.
    assert m.histogram("mg_event_loop_lag_seconds")["count"] == 2


@pytest.mark.performance
def test_the_timer_noise_floor_is_measured_not_assumed():
    """
    GIVEN an idle event loop
    WHEN  the probe calibrates
    THEN  a floor is measured and published

    Windows runs a ~15.6ms system timer, so asyncio.sleep() overshoots by
    up to that much with nothing whatsoever running. Publishing a 15ms
    reading as "event-loop lag" would be reporting the instrument rather
    than the system, so the instrument is measured first.
    """
    async def scenario():
        m = Metrics()
        monitor = sysmetrics.LoopLagMonitor(m, interval_s=0.02,
                                            calibration_probes=10)
        floor = await monitor.calibrate()
        return m, monitor, floor

    m, monitor, floor = asyncio.run(scenario())

    assert floor >= 0.0
    assert m.get_gauge("mg_event_loop_lag_floor_seconds") == pytest.approx(floor)
    # Calibration must not contaminate the measurements it calibrates.
    assert m.histogram("mg_event_loop_lag_seconds") is None
    assert monitor.max_lag == 0.0


@pytest.mark.performance
def test_loop_lag_is_never_negative_and_keeps_the_peak():
    """
    GIVEN a sleep that returned marginally early, then one that ran late
    WHEN  both are recorded
    THEN  the early one clamps to zero and the peak is retained

    Timer resolution can return a hair under the requested interval. A
    negative lag is physically meaningless and would poison the average.
    """
    m = Metrics()
    monitor = sysmetrics.LoopLagMonitor(m, interval_s=0.25)

    monitor.probe_once(0.2499)
    monitor.probe_once(0.75)
    monitor.probe_once(0.26)

    assert monitor.max_lag == pytest.approx(0.50, abs=1e-6)
    assert m.get_gauge("mg_event_loop_lag_seconds_max") == pytest.approx(0.50)
    stats = m.histogram("mg_event_loop_lag_seconds")
    assert stats["count"] == 3


@pytest.mark.performance
def test_blocking_the_event_loop_shows_up_as_lag():
    """
    GIVEN a coroutine that blocks the loop for 300ms
    WHEN  the lag probe is running
    THEN  the lag it records is large

    This is the test that justifies the metric. The gateway writes its
    audit record synchronously on the request path; this is how that cost
    becomes visible instead of being blamed on the model.
    """
    async def scenario():
        m = Metrics()
        # No calibration and no warm-up: this test is about steady state,
        # and the defaults would file the spike under startup instead.
        monitor = sysmetrics.LoopLagMonitor(
            m, interval_s=0.05, warmup_s=0.0, calibration_probes=0)
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.10)
        # Block the loop the way a synchronous call on the request path
        # would - the audit write, for instance.
        import time as _t
        _t.sleep(0.30)
        await asyncio.sleep(0.20)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return monitor.max_lag, m

    max_lag, m = asyncio.run(scenario())
    assert max_lag > 0.1, (
        f"blocking the loop for 300ms produced only {max_lag*1000:.0f}ms of "
        "measured lag - the probe is not seeing it"
    )
    # And it reached the histogram the percentiles are read from.
    assert m.histogram("mg_event_loop_lag_seconds")["count"] >= 1


# --------------------------------------------------------------------------
# The endpoints
# --------------------------------------------------------------------------


@pytest.mark.performance
def test_resources_endpoint_requires_authentication(api):
    """Host memory, core count and what else runs on the box is
    reconnaissance. It does not belong on an open endpoint."""
    client, _ = api
    assert client.get("/resources").status_code == 401


@pytest.mark.performance
def test_resources_endpoint_returns_the_three_components(api, auth_headers):
    """
    GIVEN an authenticated caller
    WHEN  /resources is read
    THEN  host, event-loop and per-process sections are present
    """
    client, _ = api
    body = client.get("/resources", headers=auth_headers).json()

    assert "host" in body and "event_loop" in body and "processes" in body
    assert "python_threads" in body["event_loop"]
    assert "tasks" in body["event_loop"]


@pytest.mark.performance
def test_healthz_says_whether_telemetry_works_without_describing_the_box(api):
    """
    GIVEN the open liveness endpoint
    WHEN  it is read without a credential
    THEN  it reports that sampling works, and nothing about the machine

    Liveness probes must not need a credential, so whatever is on /healthz
    is public. Saying "telemetry is up" is operationally useful; saying how
    much memory the host has is a gift to an attacker.
    """
    client, _ = api
    body = client.get("/healthz").json()

    assert "sampler" in body
    assert set(body["sampler"]) == {"available", "samples"}
    text = client.get("/healthz").text
    for leaked in ("memory_available_mb", "cpu_logical", "rss_mb",
                   "swap_used_percent"):
        assert leaked not in text
