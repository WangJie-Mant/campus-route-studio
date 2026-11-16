import http.client
import json
import threading
import unittest
from unittest.mock import patch
from route_studio.server import LocalServer


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = LocalServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.application.shutdown()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, auth=True, origin=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        headers = {'Content-Type': 'application/json'}
        if auth:
            headers['Cookie'] = 'campus_route_session=' + self.server.token
        if origin:
            headers['Origin'] = self.server.origin
        if method == 'POST' and (not auth or not origin):
            # Exercise header rejection without racing a second body packet against
            # the server's immediate close (Windows can otherwise reset the socket).
            headers['Expect'] = '100-continue'
            headers['Content-Length'] = str(len((body or '').encode('utf-8')))
            body = None
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        status, content = response.status, response.read()
        connection.close()
        return status, content

    def test_authentication_and_origin_required(self):
        self.assertEqual(self.request('GET', '/api/status', auth=False)[0], 403)
        self.assertEqual(self.request('GET', '/api/status')[0], 200)
        self.assertEqual(self.request('POST', '/api/control', '{}', origin=False)[0], 403)

    def test_mumu_discovery_requires_authentication(self):
        self.assertEqual(self.request("GET", "/api/mumu", auth=False)[0],403)
        with patch("route_studio.mumu.discover_mumu",return_value={"managers":[],"errors":[]}):
            status,body=self.request("GET", "/api/mumu")
        self.assertEqual(status,200)

    def test_diagnostics_require_authentication_and_are_strict_json(self):
        self.assertEqual(self.request('GET', '/api/diagnostics', auth=False)[0], 403)
        status, body = self.request('GET', '/api/diagnostics')
        self.assertEqual(status, 200)
        report = json.loads(body)
        self.assertEqual(report['scope'], 'sender-only')
        self.assertEqual(report['targetRecording'], 'unknown')
        self.assertEqual(report['samples'], [])

    def test_trace_is_authenticated_and_validates_cursor(self):
        self.assertEqual(self.request('POST', '/api/trace', '{}', auth=False)[0], 403)
        self.assertEqual(self.request('POST', '/api/trace', '{}', origin=False)[0], 403)
        status, body = self.request('POST', '/api/trace', '{}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['points'], [])
        self.assertEqual(self.request('POST', '/api/trace', '{"after":-1}')[0], 400)

    def test_harmony_operations_are_authenticated_and_refused_during_playback(self):
        for action in ('devices', 'probe', 'clear'):
            url = '/api/harmony/' + action
            self.assertEqual(self.request('POST', url, '{}', auth=False)[0], 403)
            self.assertEqual(self.request('POST', url, '{}', origin=False)[0], 403)
        with patch('route_studio.harmony_adapter.discover_harmony', return_value={'harmony': []}) as scan:
            status, raw = self.request('POST', '/api/harmony/devices', '{"hdcPath":"sdk/hdc.exe"}')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)['harmony'], [])
            scan.assert_called_once_with('sdk/hdc.exe')
            self.server.application.playback.data['state'] = 'running'
            self.assertEqual(self.request('POST', '/api/harmony/clear', '{"target":"phone"}')[0], 400)
            self.server.application.playback.data['state'] = 'idle'

    def test_android_component_readiness_is_separate_from_adb_device_discovery(self):
        with patch('route_studio.server.artifact_status', return_value={'ready': False, 'message': '缺少安卓直连组件'}), \
             patch('route_studio.server.adb_path', return_value='adb'), \
             patch('route_studio.server.list_android_devices', return_value=[{'serial': 'test-phone', 'state': 'device'}]), \
             patch('route_studio.server.importlib.util.find_spec', return_value=None):
            status, raw = self.request('GET', '/api/devices')
        data = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertTrue(data['dependencies']['adb'])
        self.assertFalse(data['dependencies']['androidDirect'])
        self.assertEqual(data['android'][0]['serial'], 'test-phone')
        self.assertIn('缺少', data['androidDirect']['message'])

    def test_file_traversal_and_invalid_json(self):
        self.assertEqual(self.request('GET', '/../README.md')[0], 404)
        self.assertEqual(self.request('POST', '/api/start', '{')[0], 400)

    def test_quick_preview_never_starts_devices_and_uses_narrow_band(self):
        config = {'route': {'points': [{'lat': 30, 'lon': 120}, {'lat': 30, 'lon': 120.001},
                                       {'lat': 30.001, 'lon': 120.001}]},
                  'motion': {'randomOffset': 2, 'randomSpacing': 30, 'speed': 3}}
        body = json.dumps(config)
        self.assertEqual(self.request('POST', '/api/preview/track', body, auth=False)[0], 403)
        self.assertEqual(self.request('POST', '/api/preview/track', body, origin=False)[0], 403)
        with patch('route_studio.playback.make_adapter') as adapter:
            status, raw = self.request('POST', '/api/preview/track', body)
            self.assertEqual(status, 200)
            preview = json.loads(raw)
            self.assertEqual(preview['source'], 'simulation-only')
            self.assertEqual(preview['laps'], 8)
            self.assertLessEqual(preview['maxOffset'], 2.000001)
            self.assertGreater(preview['maxOffset'], .1)
            self.assertLessEqual(len(preview['points']), 6001)
            self.assertAlmostEqual(preview['points'][-1]['elapsed'], preview['plannedDistance'] / 3)
            adapter.assert_not_called()
        self.assertIsNone(self.server.application.playback.thread)
        self.assertEqual(self.server.application.playback.status()['sentUpdates'], 0)

    def test_android_probe_is_authenticated_and_never_calls_install_endpoint(self):
        self.assertEqual(self.request('POST', '/api/android/probe', '{"serial":"phone"}', auth=False)[0], 403)
        with patch('route_studio.server.probe_android_direct', return_value={'probe': True, 'message': 'no APK'}) as probe:
            status, body = self.request('POST', '/api/android/probe', '{"serial":"phone"}')
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)['probe'])
            probe.assert_called_once_with('phone')
        self.assertEqual(self.request('POST', '/api/android/setup', '{"serial":"phone"}')[0], 404)

    def test_ios_probe_and_recovery_are_scoped_and_blocked_during_replay(self):
        for operation in ('probe', 'clear'):
            self.assertEqual(self.request('POST', '/api/ios/' + operation, '{}', auth=False)[0], 403)
            self.assertEqual(self.request('POST', '/api/ios/' + operation, '{}', origin=False)[0], 403)
        with patch('route_studio.ios_adapter.check_ios_device', return_value={'transportReady': True}) as check:
            for operation in ('probe', 'clear'):
                status, _ = self.request('POST', '/api/ios/' + operation, json.dumps({'udid': 'selected-ipad'}))
                self.assertEqual(status, 200)
                check.assert_called_with('selected-ipad', clear=operation == 'clear')
            check.reset_mock()
            with patch.object(self.server.application.playback, 'status', return_value={'state': 'paused'}):
                self.assertEqual(self.request('POST', '/api/ios/clear', json.dumps({'udid': 'selected-ipad'}))[0], 400)
            check.assert_not_called()

    def test_preview_and_local_only(self):
        with self.assertRaises(ValueError):
            LocalServer(('0.0.0.0', 0))
        config = {'mode': 'preview', 'route': {'points': [{'lat': 30, 'lon': 120}, {'lat': 30.001, 'lon': 120}]}}
        self.assertEqual(self.request('POST', '/api/start', json.dumps(config))[0], 200)
        self.assertEqual(self.request('POST', '/api/control', '{"action":"stop"}')[0], 200)

    def test_session_routes_require_auth_and_never_fall_back_to_another_session(self):
        for path in ('/api/sessions', '/api/session?sessionId=default', '/api/status?sessionId=default'):
            self.assertEqual(self.request('GET', path, auth=False)[0], 403)
        for path in ('/api/sessions/create', '/api/sessions/update', '/api/sessions/delete', '/api/schedule'):
            self.assertEqual(self.request('POST', path, '{}', origin=False)[0], 403)
        code, raw = self.request('POST', '/api/sessions/create', json.dumps({'name': '第二台设备'}))
        self.assertEqual(code, 200)
        ident = json.loads(raw)['id']
        self.assertEqual(self.request('GET', '/api/session?sessionId=' + ident)[0], 200)
        self.assertEqual(self.request('GET', '/api/session?sessionId=missing')[0], 400)
        self.assertEqual(self.request('GET', '/api/status?sessionId=')[0], 400)
        with patch('route_studio.server.probe_android_direct') as probe:
            self.assertEqual(self.request('POST', '/api/android/probe', json.dumps({'sessionId': 'missing', 'serial': 'phone'}))[0], 400)
            probe.assert_not_called()
        plan = {'mode': 'preview', 'route': {'points': [{'lat': 30, 'lon': 120}, {'lat': 30.001, 'lon': 120}], 'loops': 0}}
        self.assertEqual(self.request('POST', '/api/start', json.dumps({**plan, 'sessionId': ident}))[0], 200)
        _, raw = self.request('GET', '/api/status?sessionId=' + ident)
        state = json.loads(raw)
        self.assertEqual(state['sessionId'], ident)
        self.assertEqual(len(state['sessions']), 2)
        self.assertEqual(json.loads(self.request('GET', '/api/status?sessionId=default')[1])['run']['state'], 'idle')
        self.assertEqual(self.request('POST', '/api/control', json.dumps({'sessionId': 'missing', 'action': 'stop'}))[0], 400)
        self.assertEqual(self.request('POST', '/api/sessions/delete', json.dumps({'sessionId': ident}))[0], 400)
        self.assertEqual(self.request('POST', '/api/control', json.dumps({'sessionId': ident, 'action': 'stop'}))[0], 200)


if __name__ == '__main__':
    unittest.main()
