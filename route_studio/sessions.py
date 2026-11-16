"""Independent replay workers and one-shot schedules, with exclusive device ownership."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import threading
import time

from .adapters import serial_value
from .motion import Motion
from .playback import Playback
from .routes import Route


def device_keys(config):
    """Treat alternative transports to a known device as the same resource."""
    mode = config.get('mode', 'preview')
    if mode == 'preview':
        return set()
    if mode in ('android', 'avd'):
        serial = serial_value(config.get('serial'))
        serial = serial.replace('localhost:', '127.0.0.1:')
        keys = {'android:' + serial}
        if serial.startswith('emulator-') and serial[9:].isdigit():
            keys.add('android:127.0.0.1:' + str(int(serial[9:]) + 1))
        return keys
    if mode == 'ios':
        udid = config.get('udid')
        if not isinstance(udid, str) or not udid.strip():
            raise ValueError('请明确选择 iPhone / iPad 设备')
        return {'ios:' + udid.strip().replace('-', '').lower()}
    if mode == 'harmony':
        return {'harmony:' + serial_value(config.get('target'))}
    if mode == 'mumu':
        manager, index = config.get('manager'), config.get('instance')
        if not isinstance(manager, str) or not manager.strip():
            raise ValueError('请选择 MuMu 管理器')
        if isinstance(index, bool) or not str(index).isdigit() or not 0 <= int(index) <= 999:
            raise ValueError('请明确选择 MuMu 实例，编号应为 0–999')
        path = Path(manager).resolve()
        keys = {f'mumu:{str(path).casefold()}:{int(index)}'}
        # Current MuMu installs expose the ADB port in each VM's configuration.
        # Reserve that alias too, so Android direct mode cannot fight the manager.
        for root in (path.parent.parent, path.parent):
            for vm in (root / 'vms').glob(f'MuMuPlayer-*-{int(index)}'):
                try:
                    data = json.loads((vm / 'configs' / 'vm_config.json').read_text(encoding='utf-8'))
                    port = int(data['vm']['nat']['port_forward']['adb']['host_port'])
                    keys.add(f'android:127.0.0.1:{port}')
                except (OSError, ValueError, KeyError, TypeError):
                    pass
        return keys
    raise ValueError('未知输出模式')


class ReplaySessions:
    LIMIT = 16

    def __init__(self, default=None, factory=Playback, clock=time.time):
        self.lock = threading.RLock()
        self.factory, self.clock = factory, clock
        self.closed = False
        self.operations = {}
        self.records = {'default': self._record('default', '设备会话 1', default or factory())}

    @staticmethod
    def _record(ident, name, worker):
        return dict(id=ident, name=name, worker=worker, draft=None, config=None,
                    keys=set(), plan={}, scheduledAt=None, timer=None, ticket=None)

    def _get(self, ident):
        if not isinstance(ident, str) or ident not in self.records:
            raise ValueError('设备会话不存在，请刷新会话列表')
        return self.records[ident]

    @staticmethod
    def _busy(record):
        worker = record['worker']
        return record['scheduledAt'] is not None or worker.status()['state'] in Playback.ACTIVE or bool(worker.thread and worker.thread.is_alive())

    def _available(self, keys, owner=None):
        for record in self.records.values():
            if record['id'] != owner and keys & record['keys'] and self._busy(record):
                raise ValueError(f'设备已被“{record["name"]}”占用或预约，请先停止该会话')
        if any(keys & operation for operation in self.operations.values()):
            raise ValueError('该设备正在检测或清理，请稍后重试')

    def _snapshot(self, record, full=False, include_draft=True):
        run = record['worker'].status()
        if record['scheduledAt'] is not None:
            run.update(record['plan'])
            run.update(state='scheduled', scheduledAt=record['scheduledAt'],
                       mode=record['config'].get('mode', 'preview'), error=None, cleanupError=None,
                       point=None, elapsed=0, distance=0, runId=None, traceVersion=0, sentUpdates=0,
                       lastSendTime=None, transmission=None, transport=None)
        else:
            run['scheduledAt'] = None
        data = {'id': record['id'], 'name': record['name'], 'run': run}
        if full:
            if include_draft:
                data['draft'] = deepcopy(record['draft'])
        else:
            data['run'] = {key: run.get(key) for key in
                           ('state', 'mode', 'distance', 'elapsed', 'scheduledAt', 'error', 'cleanupError', 'sentUpdates')}
        config = record['config'] or {}
        data['device'] = (config.get('serial') or config.get('udid') or config.get('target') or
                          (f'MuMu #{config["instance"]}' if config.get('mode') == 'mumu' else '地图预览'))
        return data

    def list(self, full=False):
        with self.lock:
            return [self._snapshot(record, full=full) for record in self.records.values()]

    def get(self, ident='default'):
        with self.lock:
            return self._snapshot(self._get(ident), full=True)

    def worker(self, ident='default'):
        with self.lock:
            return self._get(ident)['worker']

    def status(self, ident='default'):
        with self.lock:
            return self._snapshot(self._get(ident), full=True, include_draft=False)['run']

    def create(self, name=None, draft=None):
        with self.lock:
            if self.closed:
                raise ValueError('程序正在退出')
            if len(self.records) >= self.LIMIT:
                raise ValueError(f'最多托管 {self.LIMIT} 个会话')
            ident = secrets.token_hex(12)
            record = self._record(ident, f'设备会话 {len(self.records) + 1}', self.factory())
            self.records[ident] = record
            try:
                return self.update(ident, name=name, draft=draft)
            except Exception:
                del self.records[ident]
                raise

    def update(self, ident, *, name=None, draft=None):
        with self.lock:
            record = self._get(ident)
            if name is not None:
                if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
                    raise ValueError('会话名称需要 1–40 个字符')
            if draft is not None:
                if not isinstance(draft, dict) or len(json.dumps(draft, allow_nan=False)) > 1_500_000:
                    raise ValueError('会话方案无效或过大')
                record['draft'] = deepcopy(draft)
            if name is not None:
                record['name'] = name.strip()
            return self._snapshot(record, full=True)

    def delete(self, ident):
        with self.lock:
            record = self._get(ident)
            if ident == 'default':
                raise ValueError('请保留第一个设备会话')
            if self._busy(record):
                raise ValueError('请先停止回放或取消预约，再移除会话')
            del self.records[ident]
            return {'deleted': True}

    def start(self, ident, config, start_at=None):
        config = deepcopy(config)
        route = Route.from_dict(config.get('route'))
        Motion(config.get('motion'), route.speed)
        keys = device_keys(config)
        when = None
        if start_at is not None:
            try:
                when = datetime.fromisoformat(start_at.replace('Z', '+00:00'))
                if when.tzinfo is None:
                    raise ValueError()
                delay = when.timestamp() - self.clock()
                if not 0 < delay <= 31 * 86400:
                    raise ValueError()
            except (AttributeError, TypeError, ValueError, OverflowError):
                raise ValueError('预约时间必须包含时区，且在未来 31 天以内') from None
        with self.lock:
            if self.closed:
                raise ValueError('程序正在退出')
            record = self._get(ident)
            if self._busy(record):
                raise ValueError('当前会话已有任务，请先停止或取消预约')
            self._available(keys, ident)
            if when is None:
                result = record['worker'].start(config)
                record.update(config=config, keys=keys)
                return result
            # Constructors validate selected devices/tools without opening a transport.
            record['worker'].factory(config)
            ticket = secrets.token_hex(12)
            plan = dict(infinite=route.infinite, lapDistance=route.lap_distance, completedLaps=0,
                        totalDistance=None if route.infinite else route.total_distance,
                        duration=None if route.infinite else route.duration)
            record.update(config=config, keys=keys, plan=plan,
                          scheduledAt=when.astimezone(timezone.utc).isoformat(), ticket=ticket)
            timer = threading.Timer(delay, self._fire, args=(ident, ticket))
            timer.daemon = True
            record['timer'] = timer
            timer.start()
            return self._snapshot(record, full=True)['run']

    def _fire(self, ident, ticket):
        with self.lock:
            record = self.records.get(ident)
            if self.closed or record is None or record['ticket'] != ticket:
                return
            record.update(scheduledAt=None, ticket=None, timer=None)
            try:
                self._available(record['keys'], ident)
                record['worker'].start(deepcopy(record['config']))
            except Exception as exc:
                with record['worker'].lock:
                    record['worker'].data.update(state='error', error=f'预约启动失败：{exc}')

    def control(self, ident, action):
        with self.lock:
            record = self._get(ident)
            if record['scheduledAt'] is not None:
                if action != 'stop':
                    raise ValueError('预约中的会话只能取消预约')
                record['timer'].cancel()
                record.update(scheduledAt=None, ticket=None, timer=None, keys=set())
                return record['worker'].status()
            return record['worker'].control(action)

    @contextmanager
    def device_operation(self, config):
        keys = device_keys(config)
        ticket = secrets.token_hex(12)
        with self.lock:
            if self.closed:
                raise ValueError('程序正在退出')
            self._available(keys)
            self.operations[ticket] = keys
        try:
            yield
        finally:
            with self.lock:
                self.operations.pop(ticket, None)

    def shutdown(self):
        with self.lock:
            self.closed = True
            for record in self.records.values():
                if record['timer']:
                    record['timer'].cancel()
                record.update(scheduledAt=None, ticket=None, timer=None)
            workers = [record['worker'] for record in self.records.values()]
        with ThreadPoolExecutor(max_workers=self.LIMIT) as pool:
            list(pool.map(lambda worker: worker.shutdown(), workers))
