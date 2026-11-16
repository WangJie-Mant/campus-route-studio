"""Build the bundled app_process DEX transport; no APK or signing key."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

from build_android import ROOT, discover_sdk, jdk_tool, version, run

sys.path.insert(0, str(ROOT))
from route_studio.android_artifact import ARTIFACT_NAME, validate_artifact

RESOURCE = ROOT / 'route_studio/resources' / ARTIFACT_NAME
MANIFEST = RESOURCE.with_suffix('.json')


def source_digest():
    digest = hashlib.sha256()
    for path in sorted((ROOT / 'bridge/src').rglob('*.java')):
        digest.update(path.relative_to(ROOT).as_posix().encode() + b'\0' + path.read_bytes())
    return digest.hexdigest()


def bridge_current():
    try:
        validate_artifact(RESOURCE)
        info = json.loads(MANIFEST.read_text(encoding='utf-8'))
        return (info['sourceSha256'] == source_digest() and
                info['jarSha256'] == hashlib.sha256(RESOURCE.read_bytes()).hexdigest())
    except (OSError, ValueError, KeyError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', help='Installed Android SDK (platform API 28+ and build-tools with D8)')
    args = parser.parse_args()
    sdk = discover_sdk(args.sdk)
    platforms = [p for p in (sdk / 'platforms').glob('android-*') if version(p) >= (28,) and (p / 'android.jar').is_file()]
    tools = [p for p in (sdk / 'build-tools').glob('*') if (p / 'lib/d8.jar').is_file()]
    if not platforms or not tools:
        raise RuntimeError('Install an Android SDK platform API 28+ and build-tools containing lib/d8.jar.')
    platform = max(platforms, key=version) / 'android.jar'
    dexer = max(tools, key=version) / 'lib/d8.jar'
    sources = sorted((ROOT / 'bridge/src').rglob('*.java'))
    if not sources:
        raise RuntimeError('Missing bridge/src Java sources.')
    build = ROOT / 'build'
    build.mkdir(exist_ok=True)
    RESOURCE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=build, prefix='direct-bridge-') as temp:
        tmp = Path(temp)
        classes, dex = tmp / 'classes', tmp / 'dex'
        classes.mkdir(); dex.mkdir()
        run([jdk_tool('javac'), '-J-Duser.language=en', '-J-Duser.country=US', '-encoding', 'UTF-8', '--release', '8', '-classpath', platform, '-d', classes, *sources])
        run([jdk_tool('java'), '-Duser.language=en', '-Duser.country=US', '-cp', dexer, 'com.android.tools.r8.D8', '--min-api', '26', '--lib', platform,
             '--output', dex, *sorted(classes.rglob('*.class'))])
        artifact = tmp / ARTIFACT_NAME
        with zipfile.ZipFile(artifact, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(dex.glob('*.dex')):
                entry = zipfile.ZipInfo(path.name, date_time=(2020, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, path.read_bytes())
        validate_artifact(artifact)
        pending = RESOURCE.with_suffix('.jar.tmp')
        shutil.copy2(artifact, pending)
        pending.replace(RESOURCE)
        shutil.copy2(RESOURCE, build / ARTIFACT_NAME)
    info = {'protocol': 'CRS1', 'minSdk': 26, 'sourceSha256': source_digest(),
            'jarSha256': hashlib.sha256(RESOURCE.read_bytes()).hexdigest()}
    MANIFEST.write_text(json.dumps(info, indent=2) + '\n', encoding='utf-8')
    print('Built ' + str(RESOURCE) + ' (also copied to build/; no APK installation)')


if __name__ == '__main__':
    main()
