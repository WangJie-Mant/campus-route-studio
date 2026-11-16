"""Experimental HarmonyOS 7+ HDC/testhelper transport; no Android runtime/HAP.

Protocol reference: https://developer.huawei.com/consumer/cn/doc/HarmonyOS-Guides/testhelper-guidelines/
An acknowledged command is not a location callback from the phone or target app.
"""
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

from .adapters import DeviceError, command

DEFAULT_RECEIVER = 'com.tencent.wechat'
MOCK_SCOPE = 'debug-apps-usb-only'


def receiver_value(value):
    value = DEFAULT_RECEIVER if value is None else value
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+', value) or len(value) > 255:
        raise ValueError('请填写接收定位的鸿蒙应用包名，例如 com.example.locationtest。')
    return value


def find_hdc(explicit=None):
    configured = explicit or os.environ.get('CAMPUS_ROUTE_HDC')
    if configured:
        path = Path(configured)
        if not path.is_file() or path.name.lower() not in ('hdc', 'hdc.exe'):
            raise DeviceError('HDC 工具路径无效，请选择 HarmonyOS SDK 中的 hdc.exe。')
        return str(path.resolve())
    found = shutil.which('hdc')
    if found:
        return found
    candidates = []
    for name in ('DEVECO_SDK_HOME', 'OHOS_SDK_HOME', 'HDC_HOME'):
        if os.environ.get(name):
            root = Path(os.environ[name])
            candidates.extend(root / sub for sub in ('hdc.exe', 'toolchains/hdc.exe', 'openharmony/toolchains/hdc.exe'))
    for base in (Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Huawei',
                 Path(os.environ.get('LOCALAPPDATA', '.')) / 'Huawei'):
        for pattern in ('DevEco Studio*/sdk/default/openharmony/toolchains/hdc.exe',
                        'Sdk/*/openharmony/toolchains/hdc.exe', 'Sdk/*/toolchains/hdc.exe'):
            candidates.extend(base.glob(pattern))
    for path in candidates:
        if path.is_file():
            return str(path.resolve())
    raise DeviceError('未找到 HDC。请安装华为官方 DevEco Studio / HarmonyOS SDK，并填写 hdc.exe 的完整路径。')


def target_value(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', value):
        raise ValueError('请选择明确的鸿蒙 USB 设备。')
    return value


def list_targets(hdc):
    output = command([hdc, 'list', 'targets', '-v'])
    if output.strip() == '[Empty]':
        return []
    devices = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[1].upper() == 'USB':
            devices.append({'target': target_value(fields[0]), 'state': fields[2],
                            'name': fields[3] if len(fields) > 3 else fields[0]})
    if output.strip() and not devices:
        raise DeviceError('HDC 未返回可识别的 USB 设备列表，请检查工具版本、USB 连接及调试授权。')
    return devices


def discover_harmony(hdc_path=None):
    hdc = find_hdc(hdc_path)
    return {'harmony': list_targets(hdc), 'dependencies': {'hdc': True}, 'hdcPath': hdc, 'errors': []}


def point_gpx(point):
    values = {}
    for key, low, high, default in (('lat', -90, 90, None), ('lon', -180, 180, None),
                                     ('speed', 0, 150, 0), ('bearing', 0, 360, 0)):
        raw = point.get(key, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError('鸿蒙定位字段无效：' + key) from None
        if isinstance(raw, bool) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError('鸿蒙定位字段超出范围：' + key)
        values[key] = value
    root = ET.Element('gpx', {'version': '1.1', 'creator': 'CampusRouteStudio',
                            'xmlns': 'http://www.topografix.com/GPX/1/1'})
    waypoint = ET.SubElement(root, 'wpt', {'lat': str(values['lat']), 'lon': str(values['lon'])})
    ET.SubElement(waypoint, 'time').text = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    ET.SubElement(waypoint, 'speed').text = str(values['speed'])
    ET.SubElement(waypoint, 'direction').text = str(values['bearing'])
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def track_gpx(points):
    """A finite, independently playable queue, not repeated single-fix replacement."""
    if not 2 <= len(points) <= 1000:
        raise ValueError('鸿蒙轨迹段需要 2–1000 个定位点。')
    root = ET.Element('gpx', {'version': '1.1', 'creator': 'CampusRouteStudio',
                            'xmlns': 'http://www.topografix.com/GPX/1/1'})
    segment = ET.SubElement(ET.SubElement(root, 'trk'), 'trkseg')
    started = datetime.now(timezone.utc).replace(microsecond=0)
    for index, point in enumerate(points):
        # Reuse the established field validation, then construct canonical track points.
        source = ET.fromstring(point_gpx(point))
        waypoint = source.find('{http://www.topografix.com/GPX/1/1}wpt')
        node = ET.SubElement(segment, 'trkpt', dict(waypoint.attrib))
        ET.SubElement(node, 'time').text = (started + timedelta(seconds=index)).isoformat().replace('+00:00', 'Z')
        ET.SubElement(node, 'speed').text = str(float(point.get('speed', 0)))
        ET.SubElement(node, 'direction').text = str(float(point.get('bearing', 0)))
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


class HarmonyAdapter:
    window_size = 12
    capabilities = {'coordinates': True, 'coordinateSystem': 'WGS84', 'speed': True,
                    'bearing': True, 'accuracy': False, 'motion': False,
                    'targetAppFeedback': False, 'experimental': True,
                    'mockScope': MOCK_SCOPE, 'productionAppsSupported': False}

    def __init__(self, target, hdc_path=None, receiver_bundle=None):
        self.target = target_value(target)
        self.receiver_bundle = receiver_value(receiver_bundle)
        self.receiver = None
        self.hdc = find_hdc(hdc_path)
        self.opened = self.needs_restore = self.remote_written = False
        self.local = None
        self.remote = '/data/local/tmp/campus-route-harmony-' + uuid.uuid4().hex + '.gpx'
        self.next_submission = 0.0
        self.window_key = None
        self.submissions = self.queued_points = self.last_batch_points = 0
        self.last_stage = 'idle'
        self.failure_stage = None
        self.last_batch_ms = None

    def _call(self, *args):
        try:
            return command([self.hdc, '-t', self.target, *args], timeout=10)
        except Exception:
            self.failure_stage = self.failure_stage or self.last_stage
            raise

    def _helper(self, verb, *args):
        return self._call('shell', 'testhelper', verb, *args)

    def _confirm(self, output, expected, stage):
        self.last_stage = stage
        if re.search(r'(?im)^\s*(?:error\s*:|\[fail\])', output) or expected not in output.lower():
            self.failure_stage = self.failure_stage or stage
            raise DeviceError('鸿蒙操作未确认（' + stage + '）：' + output[-500:])

    def transport_status(self):
        return {'strategy': 'segmented-gpx-v2', 'submissions': self.submissions,
                'queuedPoints': self.queued_points, 'batchPoints': self.last_batch_points,
                'deviceInterval': 1, 'nextSubmissionIn': max(0, self.next_submission - time.monotonic()) if self.opened else None,
                'expectedSubmissionInterval': self.last_batch_points + 0.5,
                'lastStage': self.last_stage, 'failureStage': self.failure_stage, 'lastBatchMs': self.last_batch_ms,
                'restorePending': self.needs_restore, 'deviceLocationFeedback': 'unavailable',
                'mockScope': MOCK_SCOPE, 'receiverStatus': self.receiver['status'] if self.receiver else 'unchecked'}

    def check_receiver(self):
        """Read only the declared recipient's debug flag; never patch app metadata."""
        self.last_stage = 'receiver-check'
        try:
            output = self._call('shell', 'bm', 'dump', '-n', self.receiver_bundle)
            start = output.find('{')
            if start < 0:
                raise ValueError('missing application metadata')
            info = json.loads(output[start:]).get('applicationInfo', {})
            debug = info.get('debug')
            if info.get('bundleName') != self.receiver_bundle or not isinstance(debug, bool):
                raise ValueError('missing or mismatched debug flag')
        except (DeviceError, ValueError, TypeError, AttributeError):
            self.receiver = {'status': 'unknown', 'debug': None, 'eligible': False,
                             'message': '无法确认接收应用已安装且为调试版，请核对包名和设备。未开启模拟定位。'}
        else:
            self.receiver = {'status': 'debug' if debug else 'release', 'debug': debug, 'eligible': debug,
                             'message': ('接收应用具有调试标记；仍需在该应用中核对定位回调。' if debug else
                                         '接收应用是正式版（debug=false）。鸿蒙官方模拟定位只向调试版应用分发；正式版微信、地图及其小程序无法通过此通道接收。未开启模拟定位。')}
        return dict(self.receiver)

    def probe(self, *, check_receiver=True):
        self.last_stage = 'probe'
        devices = list_targets(self.hdc)
        selected = next((device for device in devices if device['target'] == self.target), None)
        if not selected or selected['state'].lower() not in ('connected', 'ready'):
            raise DeviceError('所选鸿蒙 USB 设备未连接或尚未授权；不会改连其他设备。')
        help_text = self._helper('help')
        verbs = ('enable-location-mock', 'disable-location-mock', 'set-mocked-locations')
        if not all(re.search(r'(?m)^\s*' + re.escape(verb) + r'\b', help_text) for verb in verbs):
            raise DeviceError('此设备未提供完整 testhelper 定位调试命令，需要 HarmonyOS 7+；检测没有修改定位。')
        result = {'probe': True, 'mockScope': MOCK_SCOPE,
                  'message': '鸿蒙定位调试命令检测通过，未修改定位。该通道仅供 USB 连接下的调试版鸿蒙应用使用。'}
        if check_receiver:
            result['receiver'] = self.check_receiver()
            result['ready'] = self.receiver['eligible']
            result['message'] += self.receiver['message']
        return result

    def open(self):
        if self.opened:
            return
        result = self.probe()
        if not result['ready']:
            self.failure_stage = 'receiver-check'
            raise DeviceError(self.receiver['message'])
        # A timed-out command can still have reached the selected device.
        self.needs_restore = True
        self.last_stage = 'enable'
        output = self._helper('enable-location-mock')
        self._confirm(output, 'enable location mock successfully.', 'enable')
        self.opened = True

    def send(self, point):
        # One-shot compatibility entry point. Playback uses send_window below.
        return self.send_window([point] * self.window_size, key=object())

    def send_window(self, points, *, key):
        if not self.opened:
            raise DeviceError('请先打开鸿蒙定位调试连接。')
        if key == self.window_key and time.monotonic() < self.next_submission:
            return False
        payload = track_gpx(points)  # Validate the entire batch before transferring anything.
        started = time.perf_counter()
        if self.local is None:
            self.local = tempfile.TemporaryDirectory(prefix='campus-route-harmony-')
        local_file = Path(self.local.name) / 'segment.gpx'
        local_file.write_bytes(payload)
        self.remote_written = True
        self.last_stage = 'transfer'
        output = self._call('file', 'send', str(local_file), self.remote)
        self._confirm(output, 'filetransfer finish', 'transfer')
        self.last_stage = 'load'
        output = self._helper('set-mocked-locations', self.remote, '1')
        self._confirm(output, 'set mocked locations successfully.', 'load')
        self.window_key = key
        self.last_batch_points = len(points)
        self.submissions += 1
        self.queued_points += len(points)
        # Let the device consume AND finish its finite queue before normal renewal.
        # Pausing, resuming and tuning deliberately replace it via a new key.
        self.next_submission = time.monotonic() + len(points) + 0.5
        self.last_batch_ms = round((time.perf_counter() - started) * 1000)
        return True

    def close(self):
        self.opened = False
        errors = []
        if self.needs_restore:
            try:
                self.last_stage = 'restore'
                output = self._helper('disable-location-mock')
                self._confirm(output, 'disable location mock successfully.', 'restore')
                self.needs_restore = False
            except Exception as exc:
                errors.append('无法确认恢复真实定位；请重新连接该手机并点击“清除鸿蒙模拟”：' + str(exc))
        if self.remote_written:
            try:
                self._call('shell', 'rm', '-f', self.remote)
                self.remote_written = False
            except Exception as exc:
                errors.append('未确认删除本次临时定位文件：' + str(exc))
        if self.local is not None:
            try:
                self.local.cleanup()
                self.local = None
            except Exception as exc:
                errors.append('清理本机临时定位文件失败：' + str(exc))
        if errors:
            raise DeviceError('；'.join(errors))

    def clear(self):
        # Restoration must remain available even if the recipient is absent/release.
        self.probe(check_receiver=False)
        self.needs_restore = True
        self.close()
        return {'message': '鸿蒙服务已确认关闭模拟定位；请在手机核对真实位置已恢复。'}
