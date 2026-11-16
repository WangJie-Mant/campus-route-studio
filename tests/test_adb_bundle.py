import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from route_studio.adapters import adb_path
from route_studio import android_artifact


class AndroidArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = (Path(__file__).resolve().parents[1] / 'route_studio/resources/campus-route-direct.jar').read_bytes()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / 'installed/route_studio'
        self.package.mkdir(parents=True)
        patcher = patch.object(android_artifact, '__file__', str(self.package / 'android_artifact.py'))
        patcher.start(); self.addCleanup(patcher.stop)

    def put(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.payload)
        return path

    def test_installed_package_has_no_dependency_on_repository_build_folder(self):
        bundled = self.put(self.package / 'resources' / android_artifact.ARTIFACT_NAME)
        self.assertEqual(android_artifact.direct_artifact(), bundled)
        self.assertTrue(android_artifact.artifact_status()['ready'])
        self.assertFalse((self.package.parent / 'build').exists())

    def test_legacy_development_output_remains_supported(self):
        legacy = self.put(self.package.parent / 'build' / android_artifact.ARTIFACT_NAME)
        self.assertEqual(android_artifact.direct_artifact(), legacy)

    def test_frozen_runtime_uses_its_own_payload_and_never_source_fallback(self):
        self.put(self.package / 'resources' / android_artifact.ARTIFACT_NAME)
        frozen = self.root / 'frozen'
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, '_MEIPASS', str(frozen), create=True):
            with self.assertRaisesRegex(RuntimeError, 'Windows'):
                android_artifact.direct_artifact()
            for relative in ('route_studio/resources', '.'):
                payload = self.put(frozen / relative / android_artifact.ARTIFACT_NAME)
                self.assertEqual(android_artifact.direct_artifact(), payload)
                payload.unlink()

    def test_missing_and_damaged_components_are_reported_before_device_use(self):
        status = android_artifact.artifact_status()
        self.assertFalse(status['ready'])
        self.assertIn('完整源码或 Python 安装包', status['message'])
        damaged = self.package / 'resources' / android_artifact.ARTIFACT_NAME
        damaged.parent.mkdir()
        damaged.write_bytes(b'not an Android component')
        self.assertIn('损坏', android_artifact.artifact_status()['message'])

    def test_plain_java_jar_is_rejected(self):
        import zipfile
        plain = self.root / 'plain.jar'
        with zipfile.ZipFile(plain, 'w') as archive:
            archive.writestr('org/campusroute/bridge/Main.class', b'java bytecode')
        with self.assertRaises(ValueError):
            android_artifact.validate_artifact(plain)

    def test_windows_packaging_refuses_missing_or_different_transport(self):
        import importlib.util
        source = Path(__file__).resolve().parents[1] / 'scripts/build_windows.py'
        spec = importlib.util.spec_from_file_location('build_windows_checks', source)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        package = self.root / 'windows-package'
        reference = self.put(self.root / 'current.jar')
        with self.assertRaisesRegex(RuntimeError, 'Rebuild'):
            builder.verify_bundled_bridge(package, reference)
        bundled = self.put(package / '_internal/route_studio/resources' / android_artifact.ARTIFACT_NAME)
        builder.verify_bundled_bridge(package, reference)
        # Still a valid DEX archive, but not the component the build is shipping.
        import zipfile
        with zipfile.ZipFile(bundled, 'a') as archive:
            archive.writestr('old-build.txt', 'stale')
        with self.assertRaisesRegex(RuntimeError, 'Rebuild'):
            builder.verify_bundled_bridge(package, reference)


@unittest.skipUnless(sys.platform == 'win32', 'Windows bundle')
class BundledADBTests(unittest.TestCase):
    def test_server_binary_lives_outside_install_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / 'installed/platform-tools'
            tools.mkdir(parents=True)
            for name in ('adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll'):
                (tools / name).write_bytes(('fixture-' + name).encode())
            with patch.object(sys, 'frozen', True, create=True), patch.object(sys, '_MEIPASS', str(root / 'installed'), create=True), patch.dict(os.environ, {'LOCALAPPDATA': str(root / 'userdata')}):
                first = Path(adb_path())
                self.assertTrue(first.is_relative_to(root / 'userdata'))
                self.assertFalse(first.is_relative_to(root / 'installed'))
                self.assertEqual(first.read_bytes(), (tools / 'adb.exe').read_bytes())
                self.assertEqual(adb_path(), str(first))
                (tools / 'adb.exe').write_bytes(b'new-version')
                second = Path(adb_path())
                self.assertNotEqual(first, second)
                self.assertEqual(second.read_bytes(), b'new-version')
                self.assertTrue(first.is_file(), 'existing server cache must remain untouched')


if __name__ == '__main__':
    unittest.main()
