"""Explicit synthetic route variations, with distance integrated continuously."""
import math
import hashlib
import secrets
from .routes import EARTH_RADIUS, distance, number


class Motion:
    def __init__(self, data=None, speed=2.5, *, random_seed=None):
        data = data or {}
        self.mode = data.get('mode', 'fixed')
        if self.mode not in ('fixed', 'smooth', 'alternating'):
            raise ValueError('速度模式无效')
        self.speed = number(data.get('speed', speed), '速度', 0.2, 20)
        self.low = number(data.get('low', self.speed), '最低速度', 0.2, 20)
        self.high = number(data.get('high', self.speed), '最高速度', self.low, 20)
        self.period = number(data.get('period', 10), '速度周期（秒）', 2, 120)
        self.sway = number(data.get('sway', 0), '左右摆幅（米）', 0, 3)
        self.sway_period = number(data.get('swayPeriod', 4), '摆动周期（秒）', 2, 30)
        self.random_offset = number(data.get('randomOffset', 0), '贴线偏移上限（米）', 0, 5)
        self.random_spacing = number(data.get('randomSpacing', 30), '局部变化间距（米）', 10, 100)
        # One seed per run. Evaluating the same progress never consumes random state.
        self.random_seed = secrets.randbits(64) if random_seed is None else random_seed

    def speed_at(self, t):
        if self.mode == 'fixed':
            return self.speed
        if self.mode == 'alternating':
            return self.low if t % self.period < self.period / 2 else self.high
        return (self.low + self.high) / 2 - (self.high - self.low) / 2 * math.cos(2 * math.pi * t / self.period)

    def integral(self, t):
        if self.mode == 'fixed':
            return self.speed * t
        mean = (self.low + self.high) / 2
        if self.mode == 'smooth':
            return mean * t - (self.high - self.low) * self.period / (4 * math.pi) * math.sin(2 * math.pi * t / self.period)
        cycles = math.floor(t / self.period)
        rem = t - cycles * self.period
        return cycles * mean * self.period + min(rem, self.period / 2) * self.low + max(0, rem - self.period / 2) * self.high

    @staticmethod
    def _smooth(value):
        value = min(1.0, max(0.0, value))
        return value * value * (3 - 2 * value)

    def _noise(self, progress, channel):
        index = math.floor(progress)
        def value(k):
            key = f'{self.random_seed}:{channel}:{k}'.encode('ascii')
            return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), 'big') / (2**64 - 1) * 2 - 1
        weight = self._smooth(progress - index)
        return value(index) * (1 - weight) + value(index + 1) * weight

    @staticmethod
    def _normal(point, travelled, route):
        if route is None:
            angle = math.radians(point['bearing'] + 90)
            return math.sin(angle), math.cos(angle)
        # Use a local tangent instead of an old, far-away anchor's heading.
        # Wrap the window around closed tracks so the start/finish tangent agrees.
        window = min(3.0, route.lap_distance / 8)
        closed = distance(route.points[0], route.points[-1]) <= 0.01
        def sample(progress):
            progress = progress % route.lap_distance if closed else max(0, min(route.lap_distance, progress))
            return route.at(progress / route.speed)[0]
        before, after = sample(travelled - window), sample(travelled + window)
        lat = math.radians(point['lat'])
        east = math.radians((after['lon'] - before['lon'] + 180) % 360 - 180) * math.cos(lat) * EARTH_RADIUS
        north = math.radians(after['lat'] - before['lat']) * EARTH_RADIUS
        # A hairpin/reversal has no unique normal: shrink continuously through it.
        scale = max(math.hypot(east, north), window)
        return north / scale, -east / scale

    def offset(self, point, t, travelled, total, route=None):
        lateral = self.sway * math.sin(2 * math.pi * t / self.sway_period)
        if self.random_offset:
            lap_distance = route.lap_distance if route is not None else 400.0
            # A gradual lap-scale bias plus local meandering. Neither resets at a seam.
            variation = 0.55 * self._noise(travelled / lap_distance, 'lap')
            variation += 0.45 * self._noise(travelled / self.random_spacing, 'local')
            lateral += self.random_offset * variation
            # Legacy sway cannot push an enabled track band outside its chosen width.
            lateral = max(-self.random_offset, min(self.random_offset, lateral))
        fade = self._smooth(min(travelled, total - travelled) / (10 if self.random_offset else 5))
        lateral *= fade
        normal_east, normal_north = self._normal(point, travelled, route)
        east, north = normal_east * lateral, normal_north * lateral
        length = math.hypot(east, north)
        if not length:
            return point
        # Spherical destination keeps the displacement bounded, including high latitudes.
        arc, heading = length / EARTH_RADIUS, math.atan2(east, north)
        lat, lon = math.radians(point['lat']), math.radians(point['lon'])
        shifted_lat = math.asin(max(-1, min(1, math.sin(lat) * math.cos(arc) + math.cos(lat) * math.sin(arc) * math.cos(heading))))
        shifted_lon = lon + math.atan2(math.sin(heading) * math.sin(arc) * math.cos(lat), math.cos(arc) - math.sin(lat) * math.sin(shifted_lat))
        return {**point, 'lat': math.degrees(shifted_lat), 'lon': (math.degrees(shifted_lon) + 180) % 360 - 180}

    def as_dict(self):
        return dict(mode=self.mode, speed=self.speed, low=self.low, high=self.high, period=self.period, sway=self.sway, swayPeriod=self.sway_period,
                    randomOffset=self.random_offset, randomSpacing=self.random_spacing, randomStyle='track-band-v1')
