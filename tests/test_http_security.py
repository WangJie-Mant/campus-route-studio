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
        self.server.application.playback.shutdown()
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

    def test_preview_and_local_only(self):
        with self.assertRaises(ValueError):
            LocalServer(('0.0.0.0', 0))
        config = {'mode': 'preview', 'route': {'points': [{'lat': 30, 'lon': 120}, {'lat': 30.001, 'lon': 120}]}}
        self.assertEqual(self.request('POST', '/api/start', json.dumps(config))[0], 200)
        self.assertEqual(self.request('POST', '/api/control', '{"action":"stop"}')[0], 200)


if __name__ == '__main__':
    unittest.main()
