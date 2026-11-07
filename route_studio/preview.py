"""Fast, device-free multi-lap geometry from the same Motion used by playback."""
import math

from .motion import Motion
from .routes import Route, distance


def preview_track(data):
    raw = data.get('route')
    if not isinstance(raw, dict):
        raise ValueError('缺少路线设置')
    route = Route.from_dict({**raw, 'loops': 8})
    motion = Motion(data.get('motion'), route.speed)
    count = min(6000, max(64, math.ceil(route.total_distance / 1.5)))
    points = []
    max_offset = path_distance = 0.0
    for index in range(count + 1):
        progress = route.total_distance * index / count
        if motion.mode == 'fixed':
            elapsed = progress / motion.speed
        else:
            low, high = progress / motion.high, progress / motion.low
            for _ in range(28):
                middle = (low + high) / 2
                if motion.integral(middle) < progress:
                    low = middle
                else:
                    high = middle
            elapsed = (low + high) / 2
        base, _ = route.at(progress / route.speed)
        shifted = motion.offset(base, elapsed, progress, route.total_distance, route)
        max_offset = max(max_offset, distance(base, shifted))
        if points:
            path_distance += distance(points[-1], shifted)
        points.append({'seq': index + 1, 'lat': shifted['lat'], 'lon': shifted['lon'],
                       'lap': min(7, int(progress / route.lap_distance)), 'elapsed': elapsed})
    return {'points': points, 'laps': 8, 'maxOffset': max_offset,
            'plannedDistance': route.total_distance, 'pathDistance': path_distance,
            'motion': motion.as_dict(), 'source': 'simulation-only'}
