import unittest
from types import SimpleNamespace
from unittest.mock import patch

from route_studio import notify
from route_studio import playback as playback_module
from route_studio.notify import MilestoneNotifier, describe
from route_studio.playback import Playback
from route_studio.routes import Route


class ScriptedCondition:
    """Advance the worker clock at waits, with no real waiting or scheduling race."""

    def __init__(self, owner, steps):
        self.owner = owner
        self.steps = iter(steps)

    def __enter__(self):
        self.owner.lock.acquire()
        return self

    def __exit__(self, *args):
        self.owner.lock.release()

    def notify_all(self):
        pass

    def wait(self, timeout=None):
        next(self.steps)()


class FakeAdapter:
    def __init__(self):
        self.sent = []
        self.closes = 0

    def open(self):
        pass

    def send(self, point):
        self.sent.append(dict(point))

    def close(self):
        self.closes += 1


class MilestoneNotifierTests(unittest.TestCase):
    def test_each_milestone_is_announced_once_per_run(self):
        sent = []
        notifier = MilestoneNotifier(sender=lambda title, body: sent.append((title, body)) or True)
        self.assertEqual(notifier.check(4_999), [])
        self.assertEqual(notifier.check(5_010), [{"distance": 5_000.0, "delivered": True}])
        self.assertEqual(notifier.check(6_000), [])
        self.assertEqual(sent, [(notify.TITLE, "已累计 5.00 公里")])

    def test_failed_delivery_is_reported_to_the_ui(self):
        notifier = MilestoneNotifier(sender=lambda title, body: False)
        self.assertEqual(notifier.check(5_050), [{"distance": 5_000.0, "delivered": False}])

    def test_new_run_resets_announcements(self):
        sent = []
        notifier = MilestoneNotifier(sender=lambda title, body: sent.append(body) or True)
        self.assertEqual(len(notifier.check(5_100, "a")), 1)
        self.assertEqual(notifier.check(5_200, "a"), [])
        self.assertEqual(len(notifier.check(5_300, "b")), 1)
        notifier.reset("c")
        self.assertEqual(len(notifier.check(5_400, "c")), 1)
        self.assertEqual(len(sent), 3)

    def test_invalid_milestones_are_rejected(self):
        for bad in ([], [0], [-1], [True], ["fast"], [float("inf")]):
            with self.assertRaises(ValueError):
                MilestoneNotifier(bad)

    def test_non_finite_distance_is_ignored(self):
        notifier = MilestoneNotifier(sender=lambda title, body: True)
        for bad in (float("nan"), None, "x"):
            self.assertEqual(notifier.check(bad), [])

    def test_describe_formats_kilometres(self):
        self.assertEqual(describe(5_000), "已累计 5.00 公里")


class DeliveryTests(unittest.TestCase):
    def test_windows_notification_uses_a_tray_balloon(self):
        with patch.object(notify, "tray_balloon", return_value=True) as balloon:
            self.assertTrue(notify.notify_windows("标题", "正文"))
        balloon.assert_called_once_with("标题", "正文", 10)

    def test_windows_notification_reports_shell_refusal(self):
        with patch.object(notify, "tray_balloon", return_value=False):
            self.assertFalse(notify.notify_windows("标题", "正文"))

    def test_windows_notification_is_inert_off_windows(self):
        with patch.object(notify.os, "name", "posix"):
            self.assertFalse(notify.notify_windows("标题", "正文"))


class PlaybackMilestoneTests(unittest.TestCase):
    def run_playback(self, route, moments, notifier):
        worker, adapter = Playback(notifier=notifier), FakeAdapter()
        clock = [0.0]
        steps = []
        for moment in moments:
            if callable(moment):
                steps.append(lambda action=moment: action(worker))
            else:
                steps.append(lambda value=moment: clock.__setitem__(0, value))
        worker.condition = ScriptedCondition(worker, steps)
        with patch.object(playback_module, "time", SimpleNamespace(
                monotonic=lambda: clock[0], perf_counter=lambda: clock[0], time=lambda: clock[0])):
            worker._run(route, adapter)
        return worker, adapter

    def test_playback_exposes_five_kilometre_milestone_in_status(self):
        sent = []
        notifier = MilestoneNotifier(sender=lambda title, body: sent.append(body) or True)
        route = Route([{"lat": 0, "lon": 0}, {"lat": 0, "lon": 0.05}], speed=20)
        worker, adapter = self.run_playback(
            route, [100, 200, 251, lambda w: w.control("stop")], notifier)
        state = worker.status()
        self.assertEqual(sent, ["已累计 5.00 公里"])
        self.assertGreaterEqual(state["distance"], 5_000)
        self.assertEqual(state["milestone"],
                         {"distance": 5_000.0, "delivered": True, "seq": 1, "at": 251.0})
        self.assertEqual(state["milestoneSeq"], 1)
        self.assertEqual(adapter.closes, 1)

    def test_short_run_reports_no_milestone(self):
        notifier = MilestoneNotifier(sender=lambda title, body: True)
        route = Route([{"lat": 0, "lon": 0}, {"lat": 0, "lon": 0.001}], speed=2)
        worker, _ = self.run_playback(route, [1, lambda w: w.control("stop")], notifier)
        state = worker.status()
        self.assertIsNone(state["milestone"])
        self.assertEqual(state["milestoneSeq"], 0)
        self.assertLess(state["distance"], 5_000)


if __name__ == "__main__":
    unittest.main()
