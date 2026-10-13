#!/usr/bin/env python3
"""Compare effective makepkg/pacman settings against the actual host, read-only."""
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ENGINE = os.environ.get('CONTAINER_ENGINE', 'docker')
IMAGE = os.environ.get('AUR_BUILDER_TEST_IMAGE', 'aurbuilder')
EMPTY = ROOT / (ROOT.name + 'tmp') / 'host-config-empty'
EMPTY.mkdir(parents=True, exist_ok=True)
VARIABLES = '''DLAGENTS VCSCLIENTS CARCH CHOST CPPFLAGS CFLAGS CXXFLAGS LDFLAGS
LTOFLAGS RUSTFLAGS FFLAGS FCFLAGS DEBUG_CFLAGS DEBUG_CXXFLAGS DEBUG_RUSTFLAGS
DEBUG_FFLAGS BUILDENV OPTIONS DISTCC_HOSTS BUILDDIR INTEGRITY_CHECK STRIP_BINARIES
STRIP_SHARED STRIP_STATIC MAN_DIRS DOC_DIRS PURGE_TARGETS DBGSRCDIR LIB_DIRS
PACKAGER GPGKEY COMPRESSGZ COMPRESSBZ2 COMPRESSXZ COMPRESSZST COMPRESSLRZ
COMPRESSLZO COMPRESSZ COMPRESSLZ4 COMPRESSLZ PKGEXT SRCEXT'''.split()
SCRIPT = '''source /usr/share/makepkg/util/config.sh
source_makepkg_config
for variable in "$@"; do declare -p "$variable" 2>/dev/null || printf 'unset %s\\n' "$variable"; done
'''


def output(args):
    return subprocess.check_output(args, text=True, cwd=EMPTY)


def container(*args):
    return output([ENGINE, 'run', '--rm', '--workdir', '/home/builder', '--entrypoint', '/usr/bin/env', IMAGE, *args])


def declarations(values, inside=False):
    args = ['bash', '-c', SCRIPT, 'config-check', *values]
    return container(*args) if inside else output(args)


host = declarations(VARIABLES)
built = declarations(VARIABLES, inside=True)
if host != built:
    import difflib
    raise SystemExit('Effective makepkg configuration differs:\n' + ''.join(
        difflib.unified_diff(host.splitlines(True), built.splitlines(True), 'host', 'container')))
print(f'PASS: {len(VARIABLES)} effective makepkg variables/arrays exactly match the host')

makeflags = declarations(['MAKEFLAGS'])
if makeflags not in ('unset MAKEFLAGS\n', 'declare -- MAKEFLAGS=""\n'):
    assert declarations(['MAKEFLAGS'], inside=True) == makeflags
else:
    assert 'declare -- MAKEFLAGS="-j' in declarations(['MAKEFLAGS'], inside=True)
print('PASS: host MAKEFLAGS retained, with the original nproc fallback when unset')

# These were already intentionally adapted before this refactor.
ADAPTED = {'CacheDir', 'IgnorePkg', 'IgnoreGroup'}


def policies(config):
    return [line for line in config.splitlines() if line.split(' = ', 1)[0] not in ADAPTED]


assert policies(output(['pacman-conf'])) == policies(container('pacman-conf')), 'pacman policy differs'
assert container('pacman-conf', 'CacheDir').strip() == '/cache/pacman/'
print('PASS: all effective pacman policies, repositories, mirrors and NoExtract rules match; original cache/ignore adaptations retained')

for original, snapshot in [('/etc/pacman.conf', 'pacman.conf.host'),
                           ('/etc/makepkg.conf', 'makepkg.conf.host'),
                           ('/etc/pacman.d/mirrorlist', 'mirrorlist.host')]:
    assert Path(original).read_bytes() == (ROOT / snapshot).read_bytes()
fragments = {p.name: p.read_bytes() for p in Path('/etc/makepkg.conf.d').glob('*.conf')}
assert fragments == {p.name: p.read_bytes() for p in (ROOT / 'makepkg.conf.d.host').glob('*.conf')}
user_config = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'pacman/makepkg.conf'
if not user_config.is_file():
    user_config = Path.home() / '.makepkg.conf'
assert (ROOT / 'makepkg.conf.user.host').read_bytes() == (user_config.read_bytes() if user_config.is_file() else b'')
print('PASS: imported system and user configuration snapshots are byte-for-byte copies')
