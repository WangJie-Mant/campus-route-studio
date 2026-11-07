import unittest
from route_studio.motion import Motion
from route_studio.routes import distance, Route


class MotionTests(unittest.TestCase):
    def test_alternating_speed_integrates_across_switches(self):
        m = Motion({'mode': 'alternating', 'low': 2, 'high': 4, 'period': 10})
        self.assertEqual([m.speed_at(t) for t in (0, 4.99, 5, 9.99, 10)], [2, 2, 4, 4, 2])
        self.assertEqual(m.integral(10), 30)
        self.assertEqual(m.integral(11), 32)
        self.assertEqual(m.integral(6) - m.integral(4), 6)

    def test_smooth_average_and_extrema(self):
        m = Motion({'mode': 'smooth', 'low': 2, 'high': 4, 'period': 10})
        self.assertAlmostEqual(m.speed_at(0), 2)
        self.assertAlmostEqual(m.speed_at(5), 4)
        self.assertAlmostEqual(m.integral(20), 60)

    def test_sway_is_bounded_and_fades_at_endpoints(self):
        m = Motion({'sway': 2, 'swayPeriod': 4})
        point = {'lat': 30, 'lon': 120, 'bearing': 0, 'speed': 2}
        shifted = m.offset(point, 1, 10, 100)
        self.assertAlmostEqual(distance(point, shifted), 2, places=3)
        self.assertEqual(m.offset(point, 1, 0, 100), point)
        self.assertEqual(m.offset(point, 1, 100, 100), point)

    def test_invalid_parameters_rejected(self):
        for params in ({'low': 4, 'high': 2}, {'sway': 4}, {'speed': 0}, {'period': 0}, {'mode': 'unknown'},
                       {'randomOffset': -1}, {'randomOffset': 5.1}, {'randomOffset': 500}, {'randomOffset': float('nan')},
                       {'randomSpacing': 0}, {'randomSpacing': 101}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                Motion(params)

    def random_route(self, loops=10):
        return Route([{'lat': 30, 'lon': 120}, {'lat': 30, 'lon': 120.001},
                      {'lat': 30.001, 'lon': 120.001}, {'lat': 30.001, 'lon': 120}], loops=loops)

    def shifted(self, motion, route, progress):
        point, _ = route.at(progress / route.speed)
        return motion.offset(point, progress / route.speed, progress, route.total_distance, route)

    def test_random_offset_bounded_nonrepeating_and_stable_on_pause(self):
        route = self.random_route()
        motion = Motion({'randomOffset': 2, 'randomSpacing': 30}, random_seed=42)
        displacements = []
        for i in range(101):
            progress = route.total_distance * i / 100
            point, _ = route.at(progress / route.speed)
            shifted = self.shifted(motion, route, progress)
            displacements.append(distance(point, shifted))
            self.assertLessEqual(displacements[-1], 2.000001)
        self.assertGreater(max(displacements), 0.2)
        self.assertEqual(self.shifted(motion, route, 0), route.at(0)[0])
        self.assertEqual(self.shifted(motion, route, route.total_distance), route.at(route.duration)[0])
        first = self.shifted(motion, route, 1500)
        self.assertEqual(first, self.shifted(motion, route, 1500))
        self.assertGreater(distance(first, self.shifted(motion, route, 1500 + route.lap_distance)), 0.03)
        # Evaluation frequency and unrelated queries must not change the path.
        self.shifted(motion, route, 10)
        self.assertEqual(first, self.shifted(motion, route, 1500))
        other = Motion({'randomOffset': 2, 'randomSpacing': 30}, random_seed=43)
        self.assertGreater(distance(first, self.shifted(other, route, 1500)), 0.03)

    def test_track_band_continuous_at_corners_knots_and_lap_seams(self):
        route = self.random_route(loops=0)
        motion = Motion({'randomOffset': 5, 'randomSpacing': 30}, random_seed=42)
        boundaries = [990, 1020, route.lap_distance * 4]
        boundaries += [route.lap_distance * 3 + corner for corner in route.cumulative]
        for progress in boundaries:
            with self.subTest(progress=progress):
                before = self.shifted(motion, route, progress - 0.001)
                after = self.shifted(motion, route, progress + 0.001)
                self.assertLess(distance(before, after), 0.02)

    def test_random_offset_works_at_antimeridian_and_high_latitudes(self):
        motion = Motion({'randomOffset': 2}, random_seed=42)
        for lat in (-85, 0, 85):
            point = {'lat': lat, 'lon': 179.9999, 'bearing': 0}
            shifted = motion.offset(point, 400, 1000, float('inf'))
            self.assertLessEqual(distance(point, shifted), 2.000001)
            self.assertTrue(-180 <= shifted['lon'] <= 180)

    def test_disabled_random_offset_preserves_route(self):
        point = {'lat': 30, 'lon': 120, 'bearing': 0}
        self.assertEqual(Motion().offset(point, 10, 100, 1000), point)

    def test_offset_follows_local_normal_instead_of_translating_whole_track(self):
        route = self.random_route(loops=0)
        motion = Motion({'randomOffset': 2}, random_seed=42)
        # The first straight heads east: offsets must go north/south, not along it.
        for progress in (20, 30, 50, route.lap_distance + 20):
            base, _ = route.at(progress / route.speed)
            shifted = self.shifted(motion, route, progress)
            self.assertAlmostEqual(base['lon'], shifted['lon'], places=8)

    def test_legacy_sway_respects_enabled_band_and_reversals_do_not_jump(self):
        route = self.random_route(loops=0)
        motion = Motion({'randomOffset': 2, 'sway': 3}, random_seed=42)
        for progress in range(20, 800):
            base, _ = route.at(progress / route.speed)
            self.assertLessEqual(distance(base, self.shifted(motion, route, progress)), 2.000001)
        hairpin = Route([{'lat': 30, 'lon': 120}, {'lat': 30, 'lon': 120.001}], loops=0)
        turn = hairpin.lap_distance / 2
        self.assertLess(distance(self.shifted(motion, hairpin, turn - .001), self.shifted(motion, hairpin, turn + .001)), .02)
