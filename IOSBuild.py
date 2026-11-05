"""Build/verify this game's unsigned device IPA for iLoader; never launch it.

Uses an existing Unity Xcode export. The Mac build needs Xcode, not Unity,
an Apple account, a signing certificate or a provisioning profile.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import stat
import struct
import subprocess
import sys
import time
import uuid
import zipfile

BUNDLE = 'com.strawhatprototype.gomusurvivors'
VERSION, BUILD = '1.1.0', '11'
IPA_NAME = 'GomuGomuSurvivors-iPhone-unsigned-v1.1.0.ipa'
ROOT = Path(__file__).resolve().parents[1]


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=True, indent=2)
        stream.write('\n')


def safe_name(name):
    require(isinstance(name, str) and name and not name.startswith('/') and
            not any(c in name for c in ('\\', ':', '\0')) and
            all(part not in ('', '.', '..') for part in name.split('/')),
            'Unsafe archive path: ' + repr(name))
    return name


def inventory(root):
    root = Path(root)
    require(root.is_dir(), 'Missing directory: ' + str(root))
    rows, seen = [], set()
    for path in root.rglob('*'):
        require(not path.is_symlink(), 'Unexpected symlink: ' + str(path))
        if path.is_file():
            name = safe_name(path.relative_to(root).as_posix())
            require(name.casefold() not in seen, 'Case-colliding path: ' + name)
            seen.add(name.casefold())
            rows.append({'path': name, 'bytes': path.stat().st_size, 'sha256': digest(path)})
    require(rows, 'Empty input directory: ' + str(root))
    return sorted(rows, key=lambda row: row['path'])


def inventory_digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def orientations(info):
    require(set(info.get('UISupportedInterfaceOrientations', [])) == {
        'UIInterfaceOrientationLandscapeLeft', 'UIInterfaceOrientationLandscapeRight'},
        'Expected landscape left/right only')


def inspect_export(project):
    project = Path(project).resolve()
    names = ['Unity-iPhone.xcodeproj/project.pbxproj',
             'Unity-iPhone.xcodeproj/xcshareddata/xcschemes/Unity-iPhone.xcscheme',
             'Info.plist', 'Libraries/baselib.a', 'Libraries/libiPhone-lib.a',
             'Libraries/Plugins/iOS/StrawhatHaptics.mm']
    tools = []
    for arch, bee in [('arm64', 'arm64'), ('x86_64', 'x64')]:
        base = f'Il2CppOutputProject/IL2CPP/build/deploy_{arch}/'
        tools += [base + 'il2cpp', base + 'il2cpp-compile', base + f'bee_backend/mac-{bee}/bee_backend']
    for name in names + tools:
        path = project / name
        require(path.is_file() and path.stat().st_size > 0, 'Missing Unity export file: ' + name)
    text = (project / names[0]).read_text(encoding='utf-8')
    require('PRODUCT_BUNDLE_IDENTIFIER = ' + BUNDLE + ';' in text, 'Wrong game bundle ID')
    require('deploy_$HOST_ARCH' in text and '--platform=iOS' in text, 'Missing native IL2CPP build phase')
    require(any((project / 'Il2CppOutputProject/Source/il2cppOutput').glob('*.cpp')), 'Missing generated C++')
    info = plistlib.loads((project / 'Info.plist').read_bytes())
    require(info.get('CFBundleShortVersionString') == VERSION and str(info.get('CFBundleVersion')) == BUILD,
            'Wrong source version/build')
    orientations(info)
    require((project / 'Data/data.unity3d').stat().st_size > 0, 'Missing serialized Unity game assets')
    rows = inventory(project / 'Data')
    return {'bundleIdentifier': BUNDLE, 'version': VERSION, 'build': BUILD,
            'gameFiles': rows, 'gameFilesSHA256': inventory_digest(rows),
            'gameFileCount': len(rows), 'gameBytes': sum(row['bytes'] for row in rows),
            'requiredFiles': [{'path': name, 'sha256': digest(project / name)} for name in names + tools],
            'executableTools': tools, 'gameLaunched': False, 'enteredPlayMode': False}


def prepare(project, archive):
    project, archive = Path(project).resolve(), Path(archive).resolve()
    require(not archive.is_relative_to(project), 'Archive must be outside source export')
    inspection = inspect_export(project)
    rows = inventory(project)
    manifest = {'format': 'gomu-unity-ios-export-v1', 'bundleIdentifier': BUNDLE,
                'version': VERSION, 'build': BUILD, 'files': rows,
                'filesSHA256': inventory_digest(rows), 'export': inspection,
                'gameLaunched': False, 'enteredPlayMode': False}
    archive.parent.mkdir(parents=True, exist_ok=True)
    with archive.open('xb') as stream:
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.writestr('export-manifest.json', json.dumps(manifest, indent=2) + '\n')
            for row in rows:
                z.write(project / row['path'], 'ios-export/' + row['path'])
    with zipfile.ZipFile(archive) as z:
        require(z.testzip() is None, 'Source ZIP CRC verification failed')
    write_json(str(archive) + '.json', {'archive': archive.name, 'bytes': archive.stat().st_size,
               'sha256': digest(archive), 'exportFiles': len(rows), 'exportBytes': sum(r['bytes'] for r in rows),
               'filesSHA256': manifest['filesSHA256'], 'export': inspection})
    return {'archive': str(archive), 'bytes': archive.stat().st_size, 'sha256': digest(archive), 'files': len(rows)}


def extract(archive, expected_sha, output):
    require(len(expected_sha) == 64 and all(c in '0123456789abcdef' for c in expected_sha), 'Invalid expected SHA256')
    require(digest(archive) == expected_sha, 'Downloaded source SHA256 differs')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive) as z:
        infos = z.infolist()
        require(sum(i.file_size for i in infos) <= 3 * 1024**3, 'Unexpected source archive size')
        names = [safe_name(i.filename) for i in infos]
        require(len({n.casefold() for n in names}) == len(names), 'Duplicate archive entries')
        require(all(not stat.S_ISLNK(i.external_attr >> 16) for i in infos), 'Archive contains symlinks')
        manifest = json.loads(z.read('export-manifest.json'))
        require(manifest['format'] == 'gomu-unity-ios-export-v1' and manifest['bundleIdentifier'] == BUNDLE and
                manifest['version'] == VERSION and manifest['build'] == BUILD, 'Unexpected archive identity')
        require(inventory_digest(manifest['files']) == manifest['filesSHA256'], 'Manifest inventory checksum differs')
        expected = {'export-manifest.json'} | {'ios-export/' + safe_name(r['path']) for r in manifest['files']}
        require(set(names) == expected, 'Source archive entries differ from exact manifest')
        z.extractall(output)
    require(inventory(output / 'ios-export') == manifest['files'], 'Extracted source bytes differ')
    inspection = inspect_export(output / 'ios-export')
    require(inspection == manifest['export'], 'Extracted source inspection differs')
    write_json(output.parent / 'input-verification.json', {'passed': True, 'archiveSHA256': expected_sha,
               'files': len(manifest['files']), 'filesSHA256': manifest['filesSHA256'],
               'gameFilesSHA256': inspection['gameFilesSHA256'], 'gameLaunched': False})
    return {'passed': True, 'files': len(manifest['files']), 'gameFiles': inspection['gameFileCount']}


def macho(path, filetype):
    path = Path(path)
    size = path.stat().st_size
    with path.open('rb') as stream:
        magic = stream.read(4)
        slices = [(0, size)]
        if magic in (b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf'):
            count_data = stream.read(4)
            require(len(count_data) == 4, 'Truncated fat Mach-O')
            count = struct.unpack('>I', count_data)[0]
            require(1 <= count <= 16, 'Invalid fat Mach-O count')
            wide = magic[-1] == 0xbf
            length, slices = (32 if wide else 20), []
            for _ in range(count):
                data = stream.read(length)
                require(len(data) == length, 'Truncated fat slice')
                cpu, sub, at, span = struct.unpack('>IIQQ', data[:24]) if wide else struct.unpack('>IIII', data[:16])
                require(cpu == 0x0100000c and at >= 8 + count * length and span >= 32 and at + span <= size,
                        'Wrong CPU or invalid fat slice')
                require(all(at + span <= old or at >= old + extent for old, extent in slices), 'Overlapping Mach-O slices')
                slices.append((at, span))
        result = []
        for start, span in slices:
            stream.seek(start)
            header = stream.read(32)
            require(len(header) == 32 and header[:4] == b'\xcf\xfa\xed\xfe', 'Expected native 64-bit Mach-O')
            _, cpu, sub, kind, count, length, flags, reserved = struct.unpack('<8I', header)
            require(cpu == 0x0100000c and kind == filetype and count <= 65536 and length <= span - 32,
                    'Invalid Mach-O CPU/type/commands')
            commands = stream.read(length)
            require(len(commands) == length, 'Truncated load commands')
            at, platforms, signed = 0, [], False
            for _ in range(count):
                require(at + 8 <= length, 'Truncated load command')
                command, extent = struct.unpack_from('<II', commands, at)
                require(extent >= 8 and extent % 4 == 0 and at + extent <= length, 'Invalid load command')
                if command == 0x32:
                    require(extent >= 24, 'Truncated LC_BUILD_VERSION')
                    platforms.append(struct.unpack_from('<I', commands, at + 8)[0])
                if command == 0x1d:
                    require(extent >= 16, 'Truncated LC_CODE_SIGNATURE')
                    signed |= struct.unpack_from('<I', commands, at + 12)[0] > 0
                at += extent
            require(at == length and platforms == [2], 'Executable must target iOS device, not simulator/macOS')
            require(not signed, 'Unexpected code signature in unsigned build')
            result.append({'architecture': 'arm64', 'platform': 'iOS-device', 'filetype': kind, 'signed': signed})
    return {'bytes': size, 'sha256': digest(path), 'slices': result}


def validate_app(app, game_files):
    app = Path(app)
    info = plistlib.loads((app / 'Info.plist').read_bytes())
    require(info.get('CFBundleIdentifier') == BUNDLE and info.get('CFBundleShortVersionString') == VERSION and
            str(info.get('CFBundleVersion')) == BUILD, 'Built app identity/version differs')
    require(info.get('LSRequiresIPhoneOS') is True and info.get('CFBundleSupportedPlatforms') == ['iPhoneOS'] and
            info.get('DTPlatformName') == 'iphoneos', 'Not an iPhone device app')
    orientations(info)
    executable = safe_name(info['CFBundleExecutable'])
    require('/' not in executable, 'Unsafe app executable')
    for path in app.rglob('*'):
        require(not path.is_symlink(), 'Unexpected app symlink')
        require(path.name not in ('_CodeSignature', 'embedded.mobileprovision'), 'Unexpected signing material')
    main = macho(app / executable, 2)
    framework_path = app / 'Frameworks/UnityFramework.framework/UnityFramework'
    framework = macho(framework_path, 6)
    native_haptics = exported_symbol(framework_path, '_StrawhatHaptic')
    actual = inventory(app / 'Data')
    require(actual == game_files, 'Packaged Unity assets differ from exact source inventory')
    return {'bundleIdentifier': BUNDLE, 'version': VERSION, 'build': BUILD, 'minimumIOS': info.get('MinimumOSVersion'),
            'mainExecutable': main, 'unityFramework': framework, 'gameFiles': len(actual),
            'gameFilesSHA256': inventory_digest(actual), 'nativeHapticsExported': native_haptics, 'unsigned': True}


def exported_symbol(path, name):
    """Require a defined external symbol in a thin ARM64 Mach-O symbol table."""
    path = Path(path)
    size = path.stat().st_size
    with path.open('rb') as stream:
        header = stream.read(32)
        require(len(header) == 32 and header[:4] == b'\xcf\xfa\xed\xfe', 'Native symbol check requires thin ARM64 Mach-O')
        _, cpu, _, _, count, length, _, _ = struct.unpack('<8I', header)
        require(cpu == 0x0100000c and 0 < count <= 65536 and length <= size - 32, 'Invalid native symbol header')
        commands = stream.read(length)
        at, table = 0, None
        for _ in range(count):
            require(at + 8 <= len(commands), 'Truncated symbol load command')
            command, extent = struct.unpack_from('<II', commands, at)
            require(extent >= 8 and at + extent <= length, 'Invalid symbol load command')
            if command == 2:
                require(extent >= 24 and table is None, 'Invalid/duplicate LC_SYMTAB')
                table = struct.unpack_from('<4I', commands, at + 8)
            at += extent
        require(table is not None, 'Missing native symbol table')
        symoff, symbols, stroff, string_bytes = table
        require(0 < symbols <= 1000000 and symoff + symbols * 16 <= size and
                0 < string_bytes <= 32 * 1024**2 and stroff + string_bytes <= size, 'Invalid native symbol bounds')
        stream.seek(stroff)
        strings = stream.read(string_bytes)
        wanted = name.encode('ascii') + b'\0'
        index = strings.find(wanted)
        while index >= 0 and index > 0 and strings[index-1] != 0:
            index = strings.find(wanted, index+1)
        require(index >= 0, 'Native haptic symbol name missing')
        stream.seek(symoff)
        for _ in range(symbols):
            row = stream.read(16)
            require(len(row) == 16, 'Truncated native symbol')
            string_at, kind, section, description, value = struct.unpack('<IBBHQ', row)
            if string_at == index and kind & 0xe0 == 0 and kind & 0x0e == 0x0e and kind & 1 and section > 0 and value > 0:
                return True
    raise ValueError('Native haptic symbol must be defined and exported')


def package(app, ipa):
    app, ipa = Path(app), Path(ipa)
    ipa.parent.mkdir(parents=True, exist_ok=True)
    require(not ipa.exists(), 'IPA already exists')
    pending = ipa.with_name(ipa.name + '.pending-' + uuid.uuid4().hex)
    with pending.open('xb') as stream:
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for path in sorted(app.rglob('*')):
                if path.is_file():
                    z.write(path, 'Payload/' + app.name + '/' + path.relative_to(app).as_posix())
    with zipfile.ZipFile(pending) as z:
        require(z.testzip() is None, 'IPA CRC readback failed')
    os.link(pending, ipa)  # Publish without replacing an existing output.
    pending.unlink()
    sha = digest(ipa)
    with Path(str(ipa) + '.sha256').open('x', encoding='ascii') as stream:
        stream.write(sha + '  ' + ipa.name + '\n')
    return {'path': str(ipa), 'bytes': ipa.stat().st_size, 'sha256': sha, 'signed': False}


def build(project, ipa, work, report_path):
    project, ipa, work, report_path = map(lambda p: Path(p).resolve(), (project, ipa, work, report_path))
    report = {'format': 'gomu-unsigned-ios-build-v1', 'passed': False, 'gameLaunched': False,
              'enteredPlayMode': False, 'deviceLaunched': False, 'signingPerformed': False, 'ipaCreated': False}
    try:
        require(platform.system() == 'Darwin', 'Compiling the IPA requires macOS and full Xcode')
        require(all(not p.is_relative_to(project) for p in (ipa, work, report_path)), 'Output must be outside source export')
        require(not ipa.exists() and not work.exists() and not report_path.exists(), 'Use fresh build outputs')
        inspection = inspect_export(project)
        report['export'] = inspection
        require(shutil.disk_usage(project).free >= 10 * 1024**3, 'At least 10 GiB free disk space required')
        work.mkdir(parents=True, exist_ok=False)
        copied = work / 'export'
        shutil.copytree(project, copied)
        for name in inspection['executableTools']:
            path = copied / name
            path.chmod(path.stat().st_mode | 0o111)
        report['xcodeVersion'] = subprocess.check_output(['xcodebuild', '-version'], text=True).strip()
        report['sdkVersion'] = subprocess.check_output(['xcrun', '--sdk', 'iphoneos', '--show-sdk-version'], text=True).strip()
        products = work / 'Products'
        args = ['xcodebuild', '-project', str(copied / 'Unity-iPhone.xcodeproj'), '-scheme', 'Unity-iPhone',
                '-configuration', 'Release', '-sdk', 'iphoneos', '-destination', 'generic/platform=iOS',
                '-derivedDataPath', str(work / 'DerivedData'), 'ARCHS=arm64', 'ONLY_ACTIVE_ARCH=NO',
                'CODE_SIGNING_ALLOWED=NO', 'CODE_SIGNING_REQUIRED=NO', 'CODE_SIGN_IDENTITY=', 'DEVELOPMENT_TEAM=',
                'COMPILER_INDEX_STORE_ENABLE=NO', 'CONFIGURATION_BUILD_DIR=' + str(products), 'build']
        report['buildArguments'] = args
        report['sourceRevision'] = os.environ.get('GITHUB_SHA')
        report['runURL'] = (os.environ.get('GITHUB_SERVER_URL', '') + '/' + os.environ.get('GITHUB_REPOSITORY', '') +
                            '/actions/runs/' + os.environ.get('GITHUB_RUN_ID', ''))
        started = time.monotonic()
        with (work / 'xcodebuild.log').open('x', encoding='utf-8') as log:
            result = subprocess.run(args, cwd=copied, stdout=log, stderr=subprocess.STDOUT, timeout=3300, check=False)
        report['seconds'] = round(time.monotonic() - started, 2)
        report['buildExitCode'] = result.returncode
        require(result.returncode == 0, 'Xcode compilation failed; see xcodebuild.log')
        apps = list(products.glob('*.app'))
        require(len(apps) == 1, 'Expected exactly one device app')
        report['app'] = validate_app(apps[0], inspection['gameFiles'])
        report['ipa'] = package(apps[0], ipa)
        report.update(passed=True, ipaCreated=True)
    except Exception as error:
        report['error'] = str(error)
    write_json(report_path, report)
    print(json.dumps({k: v for k, v in report.items() if k not in ('export', 'buildArguments')}, indent=2))
    return 0 if report['passed'] else 1


def verify_ipa(ipa, export, report_path):
    inspection = inspect_export(export)
    ipa, report_path = Path(ipa).resolve(), Path(report_path).resolve()
    temp = report_path.parent / ('ipa-inspection-' + uuid.uuid4().hex[:10])
    temp.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(ipa) as z:
        entries = z.infolist()
        names = [safe_name(i.filename) for i in entries]
        require(len({n.casefold() for n in names}) == len(names), 'Duplicate IPA entries')
        require(all(n.startswith('Payload/') for n in names), 'Unexpected IPA root')
        require(sum(i.file_size for i in entries) < 1024**3, 'Unexpected IPA size')
        require(all(not stat.S_ISLNK(i.external_attr >> 16) for i in entries), 'Unexpected IPA symlink')
        require(z.testzip() is None, 'IPA CRC verification failed')
        require('Payload/' in names[0], 'Missing Payload')
        z.extractall(temp)
    apps = list((temp / 'Payload').glob('*.app'))
    require(len(apps) == 1, 'Expected one app in IPA')
    prefix = 'Payload/' + apps[0].name + '/'
    require(all(n.startswith(prefix) for n in names), 'IPA contains files outside its app bundle')
    info = plistlib.loads((apps[0] / 'Info.plist').read_bytes())
    executable_name = safe_name(info['CFBundleExecutable'])
    with zipfile.ZipFile(ipa) as z:
        for name in (prefix + executable_name, prefix + 'Frameworks/UnityFramework.framework/UnityFramework'):
            require((z.getinfo(name).external_attr >> 16) & 0o111, 'IPA lost executable permission: ' + name)
    app = validate_app(apps[0], inspection['gameFiles'])
    result = {'passed': True, 'ipa': ipa.name, 'bytes': ipa.stat().st_size, 'sha256': digest(ipa), 'app': app,
              'gameLaunched': False, 'enteredPlayMode': False, 'deviceLaunched': False,
              'limitation': 'Package validation does not establish installation or gameplay on a physical iPhone.'}
    write_json(report_path, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    check = sub.add_parser('inspect')
    check.add_argument('--project', type=Path, required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--project', type=Path, required=True)
    prep.add_argument('--archive', type=Path, required=True)
    unpack = sub.add_parser('extract')
    unpack.add_argument('--archive', type=Path, required=True)
    unpack.add_argument('--sha256', required=True)
    unpack.add_argument('--output', type=Path, required=True)
    compile_cmd = sub.add_parser('build')
    compile_cmd.add_argument('--project', type=Path, required=True)
    compile_cmd.add_argument('--ipa', type=Path, required=True)
    compile_cmd.add_argument('--work', type=Path, required=True)
    compile_cmd.add_argument('--report', type=Path, required=True)
    verify = sub.add_parser('verify')
    verify.add_argument('--ipa', type=Path, required=True)
    verify.add_argument('--project', type=Path, required=True)
    verify.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'build':
        return build(args.project, args.ipa, args.work, args.report)
    if args.command == 'inspect':
        result = inspect_export(args.project)
        result.pop('gameFiles')
    elif args.command == 'prepare':
        result = prepare(args.project, args.archive)
    elif args.command == 'extract':
        result = extract(args.archive, args.sha256, args.output)
    else:
        result = verify_ipa(args.ipa, args.project, args.report)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, plistlib.InvalidFileException) as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        sys.exit(1)
