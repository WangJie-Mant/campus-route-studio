"""Single-owner replay worker with monotonic timing and explicit cleanup."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from collections import deque
import secrets
import threading
import time

from .adapters import make_adapter
from .routes import Route
from .motion import Motion
from .diagnostics import TransmissionDiagnostics


class Playback:
    ACTIVE = {"starting", "running", "paused", "stopping"}

    def __init__(self, factory=make_adapter):
        self.factory = factory
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.thread = None
        self.stop_requested = False
        self.pause_requested = False
        self.shutdown_requested = False
        self.motion = None
        self.diagnostics = None
        self.trace_points = deque(maxlen=20000)
        self.data = {"state": "idle", "elapsed": 0.0, "distance": 0.0,
                     "totalDistance": 0.0, "duration": 0.0, "point": None, "error": None, "cleanupError": None,
                     "sentUpdates": 0, "lastSendTime": None, "lastSendMs": None,
                     "transmission": None, "outputCapabilities": None}

    def status(self):
        with self.lock:
            return deepcopy(self.data)

    def trace(self, after=0, run_id=None):
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError('轨迹游标必须是非负整数')
        with self.lock:
            current_id = self.data.get('runId')
            reset = run_id != current_id
            oldest = self.trace_points[0]['seq'] if self.trace_points else 1
            return {'runId': current_id, 'reset': reset,
                    'truncated': bool(self.trace_points) and (0 if reset else after) < oldest - 1,
                    'points': deepcopy([p for p in self.trace_points if reset or p['seq'] > after])}

    def report(self):
        with self.lock:
            # Explicit allowlist: a support report must not expose route coordinates or UDIDs.
            keys = ("state", "mode", "elapsed", "distance", "totalDistance", "duration",
                    "interval", "motion", "sentUpdates", "lastSendTime", "lastSendMs",
                    "transmission", "outputCapabilities", "randomSeed")
            return deepcopy({
                "schemaVersion": 1,
                "generatedAt": datetime.now(timezone.utc).isoformat(),
                "scope": "sender-only",
                "targetRecording": "unknown",
                "notes": [
                    "里程和配速来自工具计算；发送成功不能证明微信或乐跑接收、记录。",
                    "coordinateSpeed 是相邻成功发送坐标的距离/发送间隔，不是手机回传的速度。",
                    "samples 仅保留最近 120 次成功发送；累计统计覆盖本次运行。",
                ],
                "run": {**{key: self.data.get(key) for key in keys},
                        "errorPresent": bool(self.data.get("error")),
                        "cleanupErrorPresent": bool(self.data.get("cleanupError"))},
                "samples": list(self.diagnostics.samples) if self.diagnostics else [],
            })

    def start(self, config):
        route = Route.from_dict(config.get("route"))
        motion = Motion(config.get('motion'), route.speed)
        with self.condition:
            if self.shutdown_requested:
                raise ValueError("程序正在退出")
            if self.thread and self.thread.is_alive():
                raise ValueError("已有跑步任务，请先停止并等待清理完成")
            adapter = self.factory(config)
            self.stop_requested = False
            self.pause_requested = False
            self.motion = motion
            self.trace_points.clear()
            self.diagnostics = TransmissionDiagnostics(route.interval)
            self.data.update(state="starting", elapsed=0.0, distance=0.0,
                             totalDistance=None if route.infinite else route.total_distance,
                             duration=None if route.infinite else route.duration,
                             infinite=route.infinite, lapDistance=route.lap_distance, completedLaps=0,
                             point=None, error=None, cleanupError=None, sentUpdates=0, lastSendTime=None, lastSendMs=None,
                             mode=config.get("mode", "preview"),
                             runId=secrets.token_hex(8), randomSeed=str(motion.random_seed),
                             interval=route.interval, transmission=self.diagnostics.summary(),
                             outputCapabilities=deepcopy(getattr(adapter, "capabilities", None)),
                             closedAutomatically=route.closed_automatically)
            self.data['motion'] = motion.as_dict()
            self.thread = threading.Thread(target=self._run, args=(route, adapter), daemon=True, name="route-playback")
            self.thread.start()
        return self.status()

    def tune(self, data):
        with self.condition:
            if self.data['state'] not in ('running', 'paused', 'starting'):
                raise ValueError('请先开始跑步后再应用实时参数')
            # A speed-only adjustment must not replace the random path beneath the phone.
            motion = Motion({**(self.motion.as_dict() if self.motion else {}), **data},
                            random_seed=self.motion.random_seed if self.motion else None)
            self.motion = motion
            self.data['motion'] = motion.as_dict()
            self.condition.notify_all()
        return self.status()

    def control(self, action):
        with self.condition:
            state = self.data["state"]
            if action == "stop":
                if state in self.ACTIVE:
                    self.stop_requested = True
                    self.data["state"] = "stopping"
            elif action == "pause" and state == "running":
                self.pause_requested = True
            elif action == "resume" and state == "paused":
                self.pause_requested = False
            else:
                raise ValueError("当前状态不支持此操作")
            self.condition.notify_all()
        return self.status()

    def _run(self, route, adapter):
        final = "idle"
        elapsed = 0.0
        travelled = 0.0
        # Also initialise when exercised directly by transport-independent tests.
        with self.lock:
            if self.diagnostics is None:
                self.diagnostics = TransmissionDiagnostics(route.interval)
        try:
            adapter.open()
            last = time.monotonic()
            with self.lock:
                was_paused = self.pause_requested
                previous_motion = self.motion or Motion(speed=route.speed)
            while True:
                with self.condition:
                    if self.stop_requested:
                        break
                    now = time.monotonic()
                    if not was_paused:
                        delta = max(0, now - last)
                        remaining = route.total_distance - travelled
                        end = elapsed + delta
                        increment = previous_motion.integral(end) - previous_motion.integral(elapsed)
                        if increment >= remaining:
                            low, high = elapsed, end
                            for _ in range(32):
                                mid = (low + high) / 2
                                if previous_motion.integral(mid) - previous_motion.integral(elapsed) < remaining:
                                    low = mid
                                else:
                                    high = mid
                            end = high
                            increment = remaining
                        travelled += increment
                        elapsed = end
                    last = now
                    paused = self.pause_requested
                    was_paused = paused
                    motion = self.motion or previous_motion
                    previous_motion = motion
                    point, _ = route.at(travelled / route.speed)
                    point = motion.offset(point, elapsed, travelled, route.total_distance, route)
                    point['speed'] = 0 if travelled >= route.total_distance else motion.speed_at(elapsed)
                    if paused:
                        point["speed"] = 0.0
                send_started = time.perf_counter()
                adapter.send(point)
                sent_at = time.monotonic()
                send_ms = (time.perf_counter() - send_started) * 1000
                with self.condition:
                    self.diagnostics.record(point, sent_at, send_ms, elapsed, travelled)
                    self.data.update(elapsed=elapsed, distance=travelled, point=point,
                                     completedLaps=min(route.loops, int(travelled / route.lap_distance)) if not route.infinite else int(travelled / route.lap_distance),
                                     sentUpdates=self.data["sentUpdates"] + 1,
                                     lastSendTime=time.time(), lastSendMs=round(send_ms),
                                     transmission=self.diagnostics.summary())
                    self.trace_points.append({'seq': self.data['sentUpdates'],
                                              'lat': point['lat'], 'lon': point['lon'],
                                              'lap': int(travelled / route.lap_distance),
                                              'elapsed': elapsed})
                    if self.stop_requested:
                        break
                    self.data["state"] = "paused" if paused else "running"
                    if travelled >= route.total_distance:
                        final = "completed"
                        break
                    # Keep sending zero-speed fixes when paused, including Android watchdog heartbeats.
                    self.condition.wait(timeout=max(0.01, route.interval - (time.monotonic() - now)))
        except Exception as exc:
            final = "error"
            with self.lock:
                self.data["error"] = str(exc)
        finally:
            with self.lock:
                self.data["state"] = "stopping"
            try:
                adapter.close()
            except Exception as exc:
                with self.lock:
                    self.data["cleanupError"] = str(exc)
            with self.lock:
                self.data["state"] = final

    def shutdown(self):
        with self.condition:
            self.shutdown_requested = True
            self.stop_requested = True
            self.condition.notify_all()
        if self.thread:
            self.thread.join(timeout=30)
