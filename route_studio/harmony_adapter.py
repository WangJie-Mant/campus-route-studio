"""Experimental HarmonyOS 7+ HDC/testhelper transport; no Android runtime/HAP.

Protocol reference: https://developer.huawei.com/consumer/cn/doc/HarmonyOS-Guides/testhelper-guidelines/
An acknowledged command is not a location callback from the phone or target app.
"""
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid
import xml.etree.ElementTree as ET

from .adapters import DeviceError, command


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


class HarmonyAdapter:
    capabilities = {'coordinates': True, 'coordinateSystem': 'WGS84', 'speed': True,
                    'bearing': True, 'accuracy': False, 'motion': False,
                    'targetAppFeedback': False, 'experimental': True}

    def __init__(self, target, hdc_path=None):
        self.target = target_value(target)
        self.hdc = find_hdc(hdc_path)
        self.opened = self.needs_restore = self.remote_written = False
        self.local = None
        self.remote = '/data/local/tmp/campus-route-harmony-' + uuid.uuid4().hex + '.gpx'

    def _call(self, *args):
        return command([self.hdc, '-t', self.target, *args], timeout=10)

    def _helper(self, verb, *args):
        return self._call('shell', 'testhelper', verb, *args)

    def probe(self):
        devices = list_targets(self.hdc)
        selected = next((device for device in devices if device['target'] == self.target), None)
        if not selected or selected['state'].lower() not in ('connected', 'ready'):
            raise DeviceError('所选鸿蒙 USB 设备未连接或尚未授权；不会改连其他设备。')
        help_text = self._helper('help')
        verbs = ('enable-location-mock', 'disable-location-mock', 'set-mocked-locations')
        if not all(re.search(r'(?m)^\s*' + re.escape(verb) + r'\b', help_text) for verb in verbs):
            raise DeviceError('此设备未提供完整 testhelper 定位调试命令，需要 HarmonyOS 7+；检测没有修改定位。')
        return {'probe': True, 'message': '鸿蒙定位调试命令检测通过，未修改定位。开始时仍需验证系统是否允许模拟；手机及目标软件接收尚需核对。'}

    def open(self):
        if self.opened:
            return
        self.probe()
        # A timed-out command can still have reached the selected device.
        self.needs_restore = True
        output = self._helper('enable-location-mock')
        if 'enable location mock successfully.' not in output.lower():
            raise DeviceError('未确认开启鸿蒙模拟定位；设备可能不支持或服务不可用：' + output[-500:])
        self.opened = True

    def send(self, point):
        if not self.opened:
            raise DeviceError('请先打开鸿蒙定位调试连接。')
        payload = point_gpx(point)  # Validate before writing or transferring anything.
        if self.local is None:
            self.local = tempfile.TemporaryDirectory(prefix='campus-route-harmony-')
        local_file = Path(self.local.name) / 'fix.gpx'
        local_file.write_bytes(payload)
        self.remote_written = True
        output = self._call('file', 'send', str(local_file), self.remote)
        if 'filetransfer finish' not in output.lower():
            raise DeviceError('HDC 未确认定位文件传输完成：' + output[-500:])
        output = self._helper('set-mocked-locations', self.remote, '1')
        if 'set mocked locations successfully.' not in output.lower():
            raise DeviceError('鸿蒙未确认接受模拟坐标：' + output[-500:])

    def close(self):
        self.opened = False
        errors = []
        if self.needs_restore:
            try:
                output = self._helper('disable-location-mock')
                if 'disable location mock successfully.' not in output.lower():
                    raise DeviceError(output[-500:])
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
        self.probe()
        self.needs_restore = True
        self.close()
        return {'message': '鸿蒙服务已确认关闭模拟定位；请在手机核对真实位置已恢复。'}
