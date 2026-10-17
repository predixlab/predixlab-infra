from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))

from plx import agent, jobs  # noqa: E402
from plx.config import LocalConfig  # noqa: E402
from plx.paths import Home  # noqa: E402
from plx.platform import Platform  # noqa: E402
from plx.runtime import ContainerSpec  # noqa: E402
from plx.state import State, to_iso  # noqa: E402


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)


class FakeContext:
    def __init__(self, root: Path, overrides: str | None = None) -> None:
        self.home = Home(root)
        self.home.ensure()
        if overrides:
            self.home.config_file.write_text(overrides, encoding="utf-8")
        self.platform = Platform.load()
        self.config = LocalConfig(self.platform, self.home)


class ScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.started: list[tuple[str, str, datetime | None]] = []

        def create(ctx, job_name, trigger, scheduled_for=None, skip_init=False):
            self.started.append((job_name, trigger, scheduled_for))
            return mock.Mock(id=f"{job_name}-test")

        patches = [
            mock.patch.object(jobs.Execution, "create", side_effect=create),
            mock.patch.object(jobs, "start_detached"),
            mock.patch.object(jobs.JobLock, "holder", return_value=None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _fire(self, now: datetime, last_fired: dict[str, datetime] | None = None, overrides: str | None = None, desired: str = "up") -> State:
        ctx = FakeContext(Path(self.tmp.name), overrides)
        state = State(ctx.home.state_file)
        state.desired = desired
        for job, moment in (last_fired or {}).items():
            state.set_last_fired(job, moment)
        state.save()
        with mock.patch.object(agent, "utc_now", return_value=now):
            agent.Agent(ctx).fire_due_jobs()
        return State(ctx.home.state_file)

    def test_first_start_never_replays_history(self) -> None:
        state = self._fire(utc(2026, 9, 24, 3, 0))
        self.assertEqual(self.started, [])
        self.assertEqual(to_iso(state.last_fired("signal")), "2026-09-24T02:30:00Z")

    def test_on_time_trigger_uses_utc_schedule(self) -> None:
        self._fire(utc(2026, 9, 24, 2, 30, 20), {"signal": utc(2026, 9, 23, 2, 30), "options": utc(2026, 9, 23, 11, 30)})
        self.assertEqual(self.started, [("signal", "schedule", utc(2026, 9, 24, 2, 30))])

    def test_catch_up_after_sleep_within_window(self) -> None:
        self._fire(utc(2026, 9, 24, 4, 0), {"signal": utc(2026, 9, 23, 2, 30), "options": utc(2026, 9, 23, 11, 30)})
        self.assertEqual(self.started, [("signal", "catch-up", utc(2026, 9, 24, 2, 30))])

    def test_missed_beyond_window_is_not_run(self) -> None:
        state = self._fire(utc(2026, 9, 24, 9, 0), {"signal": utc(2026, 9, 23, 2, 30), "options": utc(2026, 9, 23, 11, 30)})
        self.assertEqual(self.started, [])
        self.assertEqual(to_iso(state.last_fired("signal")), "2026-09-24T02:30:00Z")

    def test_disabled_jobs_are_skipped(self) -> None:
        state = self._fire(
            utc(2026, 9, 24, 11, 30, 5),
            {"signal": utc(2026, 9, 24, 2, 30), "options": utc(2026, 9, 23, 11, 30)},
            overrides='{"jobs": {"options": {"enabled": false}}}',
        )
        self.assertEqual(self.started, [])
        self.assertEqual(to_iso(state.last_fired("options")), "2026-09-24T11:30:00Z")

    def test_jobs_do_not_run_while_the_environment_is_down(self) -> None:
        state = self._fire(utc(2026, 9, 24, 2, 30, 20), {"signal": utc(2026, 9, 23, 2, 30), "options": utc(2026, 9, 23, 11, 30)}, desired="down")
        self.assertEqual(self.started, [])
        self.assertEqual(to_iso(state.last_fired("signal")), "2026-09-24T02:30:00Z")

    def test_each_fire_time_runs_once(self) -> None:
        last = {"signal": utc(2026, 9, 23, 2, 30), "options": utc(2026, 9, 23, 11, 30)}
        self._fire(utc(2026, 9, 24, 2, 30, 5), last)
        with mock.patch.object(agent, "utc_now", return_value=utc(2026, 9, 24, 2, 30, 20)):
            ctx = FakeContext(Path(self.tmp.name))
            agent.Agent(ctx).fire_due_jobs()
        self.assertEqual([entry[1] for entry in self.started], ["schedule"])


class ResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ctx = FakeContext(Path(self.tmp.name))
        state = State(self.ctx.home.state_file)
        state.desired = "up"
        state.save()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_failing_restart_backs_off_and_other_services_continue(self) -> None:
        broken = mock.Mock(key="broken", liveness_failures=3)
        fine = mock.Mock(key="fine", liveness_failures=3)
        started = []

        def start(ctx, service, log=None):
            started.append(service.key)
            if service.key == "broken":
                raise RuntimeError("port already in use")
            return True

        with mock.patch.object(agent.services, "ordered", return_value=[broken, fine]), \
                mock.patch.object(agent.services, "missing_image", return_value=None), \
                mock.patch.object(agent.services, "is_running", return_value=False), \
                mock.patch.object(agent.services, "start", side_effect=start):
            runner = agent.Agent(self.ctx)
            runner.reconcile(State(self.ctx.home.state_file))
        self.assertEqual(started, ["broken", "fine"])
        self.assertIn("broken", runner.backoff_until)
        self.assertNotIn("fine", runner.backoff_until)

    def test_restart_is_skipped_after_plx_down(self) -> None:
        service = mock.Mock(key="api", liveness_failures=3)
        stale = State(self.ctx.home.state_file)
        down = State(self.ctx.home.state_file)
        down.desired = "down"
        down.save()
        with mock.patch.object(agent.services, "ordered", return_value=[service]), \
                mock.patch.object(agent.services, "missing_image", return_value=None), \
                mock.patch.object(agent.services, "is_running", return_value=False), \
                mock.patch.object(agent.services, "start") as start:
            agent.Agent(self.ctx).reconcile(stale)
        start.assert_not_called()

    def test_mongo_address_change_restarts_healthy_dependent(self) -> None:
        service = mock.Mock(key="campaign", liveness_failures=3)
        with mock.patch.object(agent.services, "ordered", return_value=[service]), \
                mock.patch.object(agent.services, "missing_image", return_value=None), \
                mock.patch.object(agent.services, "is_running", return_value=True), \
                mock.patch.object(agent.services, "uses_current_mongo", return_value=False), \
                mock.patch.object(agent.services, "start", return_value=True) as start:
            runner = agent.Agent(self.ctx)
            runner.reconcile(State(self.ctx.home.state_file))
        start.assert_called_once()
        self.assertEqual(runner.failures["campaign"], 0)

    def test_jobs_fire_even_when_reconcile_fails(self) -> None:
        runner = agent.Agent(self.ctx)
        with mock.patch.object(agent.Agent, "_prepare", return_value=True), \
                mock.patch.object(agent.Agent, "_reconcile_step", side_effect=RuntimeError("boom")), \
                mock.patch.object(agent.Agent, "fire_due_jobs") as fire, \
                mock.patch.object(agent.Agent, "_prune_step"):
            runner.run(once=True)
        fire.assert_called_once()


class MarketHoursTests(unittest.TestCase):
    def test_nyse_regular_hours(self) -> None:
        self.assertTrue(jobs.us_market_open(utc(2026, 9, 23, 17, 0)))   # Wed 13:00 EDT
        self.assertFalse(jobs.us_market_open(utc(2026, 9, 24, 2, 30)))  # Wed 22:30 EDT
        self.assertFalse(jobs.us_market_open(utc(2026, 9, 26, 17, 0)))  # Saturday

    def test_only_collector_jobs_are_guarded(self) -> None:
        platform = Platform.load()
        self.assertTrue(jobs.collects_market_data(platform.job("signal")))
        self.assertFalse(jobs.collects_market_data(platform.job("options")))


class ContainerExecutionTests(unittest.TestCase):
    def test_scheduled_jobs_launch_each_step_as_a_container(self) -> None:
        platform = Platform.load()
        ctx = mock.Mock()
        ctx.container.image_exists.return_value = True
        ctx.container.run_attached.return_value = 0
        execution = mock.Mock(id="predixlab-job-test123", data={"skipInit": False, "containers": []})
        with mock.patch.object(
            jobs, "container_spec",
            side_effect=lambda _ctx, _job, container, name, _labels: ContainerSpec(name=name, image=container["image"]),
        ):
            for job_name, expected in (
                ("signal", [("collector", "init"), ("signals", "main")]),
                ("options", [("options", "main")]),
            ):
                ctx.container.run_attached.reset_mock()
                execution.data["containers"] = []
                status, reason = jobs._run_replica(
                    ctx, platform.job(job_name), execution, time.time() + 60,
                    lambda _source, _line: None, threading.Event(), {"container": None},
                )
                self.assertEqual((status, reason), ("Succeeded", None))
                self.assertEqual(
                    [(call.args[0].image, record["kind"]) for call, record in zip(
                        ctx.container.run_attached.call_args_list, execution.data["containers"]
                    )],
                    expected,
                )
                self.assertEqual([record["exitCode"] for record in execution.data["containers"]], [0] * len(expected))


if __name__ == "__main__":
    unittest.main()
