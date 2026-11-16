"""Locate the shipped Android DEX transport independently of the working directory."""
from pathlib import Path
import sys
import zipfile

ARTIFACT_NAME = 'campus-route-direct.jar'
MAIN_DESCRIPTOR = b'Lorg/campusroute/bridge/Main;'


def validate_artifact(path):
    try:
        with zipfile.ZipFile(path) as archive:
            dex = archive.read('classes.dex')
            if len(dex) < 112 or not dex.startswith(b'dex\n') or dex[7:8] != b'\0' or MAIN_DESCRIPTOR not in dex:
                raise ValueError('missing Android DEX entry point')
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        raise ValueError('安卓直连组件不是有效的 DEX JAR。') from exc


def direct_artifact():
    if getattr(sys, 'frozen', False):
        root = Path(sys._MEIPASS)
        candidates = [root / 'route_studio/resources' / ARTIFACT_NAME, root / ARTIFACT_NAME]
        remedy = '请重新安装包含安卓直连组件的完整 Windows 版。'
    else:
        package = Path(__file__).resolve().parent
        candidates = [package / 'resources' / ARTIFACT_NAME,
                      package.parent / 'build' / ARTIFACT_NAME, package.parent / ARTIFACT_NAME]
        remedy = '请使用包含 route_studio/resources 的完整源码或 Python 安装包；维护者可运行 python scripts/build_android_bridge.py 重新构建。'
    invalid = False
    for path in candidates:
        if path.is_file():
            try:
                validate_artifact(path)
                return path
            except ValueError:
                invalid = True
    raise RuntimeError(('安卓直连组件损坏。' if invalid else '缺少安卓直连组件。') + remedy)


def artifact_status():
    try:
        direct_artifact()
        return {'ready': True}
    except RuntimeError as exc:
        return {'ready': False, 'message': str(exc)}
