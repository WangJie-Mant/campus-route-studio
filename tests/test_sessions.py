import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from route_studio.playback import Playback
from route_studio.sessions import ReplaySessions, device_keys


def config(serial='phone-a', lat=30):
    return {'mode': 'android', 'serial': serial,
            'route': {'points': [{'lat': lat, 'lon': 120}, {'lat': lat, 'lon': 120.01}],
                      'speed': 2, 'loops': 0, 'interval': .2}, 'motion': {'randomOffset': 0}}


class MemoryAdapter:
    def __init__(self, fail=False):
        self.fail = fail
        self.opened = self.closed = 0
        self.sent = []
        self.updated = threading.Event()

    def open(self):
        self.opened += 1
        if self.fail:
            raise RuntimeError('disconnected test device')

    def send(self, point):
        self.sent.append(deepcopy(point))
        self.updated.set()

    def close(self):
        self.closed += 1


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.adapters = {}
        def adapter_factory(data):
            adapter = MemoryAdapter(data.get('fail', False))
            self.adapters.setdefault(data.get('serial'), []).append(adapter)
            return adapter
        self.sessions = ReplaySessions(factory=lambda: Playback(factory=adapter_factory))
        self.a = 'default'
        self.b = self.sessions.create('设备 B')['id']
        self.addCleanup(self.sessions.shutdown)

    def wait_state(self, ident, state):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.sessions.get(ident)['run']['state'] == state:
                return
            time.sleep(.01)
        self.fail(f'{ident} did not reach {state}: {self.sessions.get(ident)["run"]}')

    def test_two_devices_keep_independent_output_progress_tuning_and_trace(self):
        self.sessions.start(self.a, config())
        self.sessions.start(self.b, config('phone-b', 31))
        self.wait_state(self.a, 'running'); self.wait_state(self.b, 'running')
        self.sessions.control(self.a, 'pause')
        self.wait_state(self.a, 'paused')
        self.sessions.worker(self.a).tune({'speed': 4})
        self.assertEqual(self.sessions.get(self.b)['run']['state'], 'running')
        self.assertEqual(self.sessions.get(self.b)['run']['motion']['speed'], 2)
        a_trace, b_trace = self.sessions.worker(self.a).trace(), self.sessions.worker(self.b).trace()
        self.assertNotEqual(a_trace['runId'], b_trace['runId'])
        self.assertTrue(all(abs(p['lat'] - 30) < .001 for p in a_trace['points']))
        self.assertTrue(all(abs(p['lat'] - 31) < .001 for p in b_trace['points']))
        self.sessions.control(self.a, 'stop')
        self.wait_state(self.a, 'idle')
        self.assertEqual(self.sessions.get(self.b)['run']['state'], 'running')
        self.assertEqual(self.adapters['phone-a'][-1].closed, 1)
        self.assertEqual(self.adapters['phone-b'][-1].closed, 0)

    def test_same_device_cannot_be_started_in_two_sessions_or_transports(self):
        self.sessions.start(self.a, config('emulator-5554'))
        for target in (config('emulator-5554'), {**config('emulator-5554'), 'mode': 'avd'}, config('localhost:5555')):
            with self.subTest(target=target['serial']), self.assertRaisesRegex(ValueError, '占用'):
                self.sessions.start(self.b, target)

    def test_simultaneous_claims_for_one_device_have_exactly_one_owner(self):
        gate = threading.Barrier(2)
        results = []
        def start(ident):
            gate.wait(timeout=2)
            try:
                self.sessions.start(ident, config())
                results.append('started')
            except ValueError:
                results.append('refused')
        threads = [threading.Thread(target=start, args=(ident,)) for ident in (self.a, self.b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertCountEqual(results, ['started', 'refused'])
        self.assertEqual(len(self.adapters['phone-a']), 1)

    def test_failed_device_does_not_stop_other_worker(self):
        self.sessions.start(self.a, config())
        self.wait_state(self.a, 'running')
        self.sessions.start(self.b, {**config('phone-b'), 'fail': True})
        self.wait_state(self.b, 'error')
        self.assertIn('disconnected', self.sessions.get(self.b)['run']['error'])
        self.assertEqual(self.sessions.get(self.a)['run']['state'], 'running')
        self.assertEqual(self.adapters['phone-b'][-1].closed, 1)

    def test_schedules_fire_separately_with_frozen_payloads(self):
        first, second = config(), config('phone-b', 31)
        at = datetime.now(timezone.utc)
        self.sessions.start(self.a, first, (at + timedelta(seconds=.15)).isoformat())
        self.sessions.start(self.b, second, (at + timedelta(seconds=.65)).isoformat())
        first['serial'] = 'wrong-device'; first['route']['points'][0]['lat'] = 80
        second['route']['points'][0]['lat'] = 81
        self.assertEqual(self.sessions.get(self.a)['run']['state'], 'scheduled')
        self.wait_state(self.a, 'running')
        self.assertEqual(self.sessions.get(self.b)['run']['state'], 'scheduled')
        self.wait_state(self.b, 'running')
        self.assertTrue(all(abs(p['lat'] - 30) < .001 for p in self.adapters['phone-a'][-1].sent))
        self.assertTrue(all(abs(p['lat'] - 31) < .001 for p in self.adapters['phone-b'][-1].sent))
        self.assertNotIn('wrong-device', self.adapters)

    def test_cancelled_schedule_never_opens_device_even_if_callback_was_queued(self):
        when = (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat()
        self.sessions.start(self.a, config(), when)
        ticket = self.sessions.records[self.a]['ticket']
        with self.assertRaisesRegex(ValueError, '占用或预约'):
            self.sessions.start(self.b, config())
        self.sessions.control(self.a, 'stop')
        self.sessions._fire(self.a, ticket)
        self.assertTrue(all(adapter.opened == 0 for adapter in self.adapters['phone-a']))
        self.sessions.start(self.b, config())
        self.wait_state(self.b, 'running')

    def test_bad_times_and_unknown_session_never_fall_back_to_default(self):
        for when in ('bad', '2026-11-07T10:00', '2020-01-01T00:00:00Z', True,
                     (datetime.now(timezone.utc) + timedelta(days=32)).isoformat()):
            with self.subTest(when=when), self.assertRaises(ValueError):
                self.sessions.start(self.a, config(), when)
        with self.assertRaisesRegex(ValueError, '不存在'):
            self.sessions.start('stale-session', config())
        self.assertIsNone(self.sessions.worker().thread)

    def test_device_probe_reserves_only_its_own_target(self):
        self.sessions.start(self.a, config())
        with self.assertRaisesRegex(ValueError, '占用'):
            with self.sessions.device_operation(config()):
                self.fail('probe must not touch a running device')
        with self.sessions.device_operation(config('phone-b')):
            with self.assertRaisesRegex(ValueError, '正在检测'):
                self.sessions.start(self.b, config('phone-b'))
        self.sessions.start(self.b, config('phone-b'))
        self.wait_state(self.b, 'running')

    def test_drafts_are_copied_and_sessions_can_only_be_deleted_when_idle(self):
        draft = {'points': [{'lat': 30, 'lon': 120}], 'saved': {'params': {'speed': 3}}}
        self.sessions.update(self.a, draft=draft)
        draft['points'][0]['lat'] = 50
        returned = self.sessions.get(self.a)
        returned['draft']['points'][0]['lat'] = 60
        self.assertEqual(self.sessions.get(self.a)['draft']['points'][0]['lat'], 30)
        self.assertIsNone(self.sessions.get(self.b)['draft'])
        self.sessions.start(self.b, config('phone-b'))
        with self.assertRaises(ValueError):
            self.sessions.delete(self.b)
        with self.assertRaises(ValueError):
            self.sessions.delete(self.a)

    def test_shutdown_cancels_all_timers_and_stops_all_workers(self):
        self.sessions.start(self.a, config())
        self.wait_state(self.a, 'running')
        self.sessions.start(self.b, config('phone-b'), (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat())
        ticket = self.sessions.records[self.b]['ticket']
        self.sessions.shutdown()
        self.sessions._fire(self.b, ticket)
        self.assertTrue(all(adapter.opened == 0 for adapter in self.adapters['phone-b']))
        self.assertEqual(self.adapters['phone-a'][-1].closed, 1)
        with self.assertRaisesRegex(ValueError, '正在退出'):
            self.sessions.start(self.a, config())

    def test_mumu_instance_reserves_its_adb_alias_and_case_normalizes_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'vms' / 'MuMuPlayer-15.0-0' / 'configs'
            path.mkdir(parents=True)
            (path / 'vm_config.json').write_text(json.dumps({'vm': {'nat': {'port_forward': {'adb': {'host_port': '16384'}}}}}))
            target = {'mode': 'mumu', 'manager': str(root / 'nx_main' / 'MuMuManager.exe'), 'instance': 0}
            self.assertIn('android:127.0.0.1:16384', device_keys(target))
            self.assertEqual(device_keys(config('localhost:16384')), device_keys(config('127.0.0.1:16384')))


if __name__ == '__main__':
    unittest.main()
