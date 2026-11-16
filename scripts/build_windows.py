"""Build a standalone Windows x64 package and per-user Inno Setup installer.

Build prerequisites only: Python 3.12 x64, PyInstaller, requirements-ios.txt,
Android platform-tools, the direct DEX JAR and Inno Setup 6. End users need none of these.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile
from importlib import metadata
import importlib.util

ROOT = Path(__file__).resolve().parents[1]


def prepare_bridge(sdk):
    from build_android_bridge import RESOURCE, bridge_current
    if not bridge_current():
        subprocess.run([sys.executable, str(ROOT / 'scripts/build_android_bridge.py'), '--sdk', str(sdk)], check=True)
    return RESOURCE


def verify_bundled_bridge(package, reference):
    sys.path.insert(0, str(ROOT))
    from route_studio.android_artifact import ARTIFACT_NAME, validate_artifact
    candidates = [package / '_internal/route_studio/resources' / ARTIFACT_NAME,
                  package / '_internal' / ARTIFACT_NAME]
    for candidate in candidates:
        if candidate.is_file():
            validate_artifact(candidate)
            if candidate.read_bytes() == reference.read_bytes():
                return
    raise RuntimeError('The frozen runtime is missing the current Android direct component. Rebuild without --skip-freeze.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iscc', type=Path, required=True)
    parser.add_argument('--sdk', type=Path, default=Path(os.environ.get('ANDROID_HOME', '')))
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    parser.add_argument('--skip-freeze', action='store_true', help='Repackage an already compiled runtime')
    parser.add_argument('--work', type=Path, default=ROOT / 'build', help='Scratch directory for PyInstaller')
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    from route_studio import __version__
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    tools = args.sdk.resolve() / 'platform-tools'
    if not (tools / 'adb.exe').is_file():
        raise RuntimeError('Install Android platform-tools and pass its SDK root with --sdk.')
    bridge = prepare_bridge(args.sdk.resolve())
    if not args.skip_freeze:
        subprocess.run([
            sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
            '--onedir', '--windowed', '--name', 'CampusRouteStudio',
            '--paths', str(ROOT), '--distpath', str(output),
            '--workpath', str(args.work / 'windows'), '--specpath', str(args.work),
            '--add-data', f'{ROOT / "route_studio/web"};route_studio/web',
            '--add-data', f'{bridge.parent};route_studio/resources',
            '--add-data', f'{ROOT / "examples"};examples',
            '--add-data', f'{tools};platform-tools',
            '--collect-all', 'pymobiledevice3', '--collect-all', 'developer_disk_image',
            '--collect-all', 'pytun_pmd3',
            '--hidden-import', 'route_studio.mumu', '--hidden-import', 'route_studio.ios_adapter',
            # The build interpreter may carry several Qt bindings; the app uses none of them.
            '--exclude-module', 'PyQt5', '--exclude-module', 'PyQt6',
            '--exclude-module', 'PySide2', '--exclude-module', 'PySide6',
            # Scientific/plotting stacks are pulled in by the development interpreter only.
            '--exclude-module', 'numpy', '--exclude-module', 'scipy',
            '--exclude-module', 'pandas', '--exclude-module', 'matplotlib',
            '--exclude-module', 'pygame', '--exclude-module', 'tkinter',
            '--exclude-module', 'IPython', '--exclude-module', 'jupyter',
            '--exclude-module', 'notebook', '--exclude-module', 'numba',
            str(ROOT / 'scripts/windows_entry.py'),
        ], check=True)
    package = output / 'CampusRouteStudio'
    verify_bundled_bridge(package, bridge)
    # ctypes loads these architecture-specific DLLs by a runtime path, so retain
    # the exact package directory layout, including when repackaging a runtime.
    tunnel_source = Path(importlib.util.find_spec('pytun_pmd3').origin).parent
    shutil.copytree(tunnel_source / 'wintun/bin', package / '_internal/pytun_pmd3/wintun/bin', dirs_exist_ok=True)
    for name in ('LICENSE', 'README.md', 'MOBILE_SETUP.md', 'VALIDATION.md', 'COMPATIBILITY_VALIDATION.md', 'BACKGROUND_OPERATION.md', 'WINDOWS_DIRECT.md', 'DIRECT_RUNTIME_VALIDATION.md', 'PIP_COMPATIBILITY.md', 'ANDROID_FIX_NOTES.md', 'THIRD_PARTY_NOTICES.md'):
        shutil.copy2(ROOT / name, package / name)
    shutil.copytree(ROOT / 'docs/images', package / 'docs/images', dirs_exist_ok=True)
    licenses = package / '_internal/licenses'
    licenses.mkdir(parents=True, exist_ok=True)
    for distribution in metadata.distributions():
        for relative in distribution.files or []:
            if any(word in relative.name.lower() for word in ('license', 'copying', 'notice')):
                source = Path(distribution.locate_file(relative))
                if source.is_file():
                    target = licenses / distribution.metadata['Name'] / relative.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.is_file():
        shutil.copy2(python_license, licenses / 'Python-LICENSE.txt')
    subprocess.run([
        str(args.iscc.resolve()), f'/DAppVersion={__version__}',
        f'/DPackageDir={package}', f'/DOutputDir={output}',
        str(ROOT / 'scripts/windows_installer.iss'),
    ], check=True)
    with zipfile.ZipFile(output / 'CampusRouteStudio-Windows.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in package.rglob('*'):
            if path.is_file():
                archive.write(path, Path('CampusRouteStudio') / path.relative_to(package))


if __name__ == '__main__':
    main()
