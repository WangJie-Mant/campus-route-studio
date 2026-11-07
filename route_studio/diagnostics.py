"""Bounded sender telemetry. No device/app location callbacks are inferred here."""
from collections import deque

from .routes import distance


class TransmissionDiagnostics:
    def __init__(self, interval):
        self.gap_threshold = max(3.0, interval * 2.5)
        self.previous = None
        self.first_time = None
        self.samples = deque(maxlen=120)
        self.data = {
            "sentDistance": 0.0, "lastStepDistance": None, "coordinateSpeed": None,
            "lastIntervalMs": None, "maxIntervalMs": None, "maxSendMs": 0.0,
            "longGaps": 0, "gapThresholdMs": self.gap_threshold * 1000,
        }

    def record(self, point, timestamp, send_ms, elapsed, planned_distance):
        if self.first_time is None:
            self.first_time = timestamp
        if self.previous is not None:
            previous_point, previous_time = self.previous
            interval = max(0.0, timestamp - previous_time)
            step = distance(previous_point, point)
            self.data.update(
                sentDistance=self.data["sentDistance"] + step,
                lastStepDistance=step,
                coordinateSpeed=step / interval if interval > 0 else None,
                lastIntervalMs=interval * 1000,
                maxIntervalMs=max(self.data["maxIntervalMs"] or 0, interval * 1000),
                longGaps=self.data["longGaps"] + int(interval > self.gap_threshold),
            )
        self.data["maxSendMs"] = max(self.data["maxSendMs"], send_ms)
        # Relative times and distances suffice for cadence analysis; omit coordinates/UDIDs.
        self.samples.append({
            "time": round(timestamp - self.first_time, 3), "elapsed": round(elapsed, 3),
            "plannedDistance": round(planned_distance, 3),
            "stepDistance": self.data["lastStepDistance"],
            "intervalMs": self.data["lastIntervalMs"], "sendMs": round(send_ms, 3),
        })
        self.previous = (dict(point), timestamp)

    def summary(self):
        return dict(self.data)
