#!/usr/bin/env python3
"""Regression checks. Fixtures stay in the project's named tmp directory.

AUR_BUILDER_TEST_IMAGE=aurbuilder python3 tests/test_builder.py
Container checks require a built image; packages and signing keys are disposable.
Launcher/Retry checks need only Python's standard library and basic shell tools.
"""
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / (ROOT.name + 'tmp')
STATE.mkdir(exist_ok=True)
IMAGE = os.environ.get('AUR_BUILDER_TEST_IMAGE', 'aurbuilder')
ENGINE = os.environ.get('CONTAINER_ENGINE', 'docker')


def executable(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    path.chmod(0o755)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix='case with spaces-', dir=STATE))
        self.env = dict(os.environ, TEST_STATE=str(self.directory), LC_ALL='C')
        self.env.pop('AUR_BUILDER_CONTAINER', None)

    def tearDown(self):
        shutil.rmtree(self.directory)

    def run_command(self, args, **kwargs):
        return subprocess.run(args, env=self.env, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=120, **kwargs)

    def successful(self, result):
        self.assertEqual(result.returncode, 0, result.stdout)

    def failed(self, result, message):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, result.stdout)


class Launcher(Fixture):
    def setUp(self):
        super().setUp()
        shutil.copy2(ROOT / 'run.sh', self.directory / 'run.sh')
        self.engine = self.directory / 'bin/docker'
        executable(self.engine, '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state = Path(os.environ['TEST_STATE'])
with (state / 'engine.calls').open('a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
mode = os.environ.get('TEST_MODE', '')
if mode == sys.argv[1] + '-fail':
    sys.exit(37)
''')
        self.env['CONTAINER_ENGINE'] = str(self.engine)
        self.env['AUR_BUILDER_IMAGE'] = 'aurbuilder-offline-test'

    def launch(self, *args, input=''):
        return self.run_command(['bash', str(self.directory / 'run.sh'), *args], input=input)

    def calls(self):
        path = self.directory / 'engine.calls'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_help_has_no_side_effects(self):
        self.successful(self.launch('--help'))
        self.assertFalse(self.calls())
        self.assertFalse((self.directory / 'cache').exists())

    def test_invalid_commands_and_targets_never_reach_engine(self):
        for args in [('oops',), ('build',), ('build', '--overwrite'), ('build', '../x'),
                     ('build', 'a b'), ('clean', '--pkg'), ('clean', '--pkg', '..'),
                     ('clean', '--unknown')]:
            with self.subTest(args=args):
                self.assertNotEqual(self.launch(*args).returncode, 0)
        self.assertFalse(self.calls())

    def test_new_workspace_and_multiple_targets(self):
        self.successful(self.launch('build', 'foo+bar', 'alpha'))
        self.assertEqual((self.directory / 'cache/gnupg').stat().st_mode & 0o777, 0o700)
        calls = self.calls()
        self.assertEqual([c[0] for c in calls], ['info', 'build', 'run'])
        self.assertEqual(calls[-1][-4:], ['aurbuilder-offline-test', 'build', 'foo+bar', 'alpha'])
        self.assertIn(str(self.directory / 'cache') + ':/cache', calls[-1])
        self.assertNotIn('-t', calls[-1])
        self.assertNotIn('-i', calls[-1])
        host = Path('/etc/pacman.conf')
        self.assertEqual((self.directory / 'pacman.conf.host').read_bytes(), host.read_bytes() if host.is_file() else b'')

    def test_host_config_is_refreshed_on_launch(self):
        path = self.directory / 'pacman.conf.host'
        path.write_text('do not overwrite\n')
        self.successful(self.launch())
        host = Path('/etc/pacman.conf')
        self.assertEqual(path.read_bytes(), host.read_bytes() if host.is_file() else b'')
        self.assertEqual(self.calls()[-1][-1], 'aurbuilder-offline-test')

    def test_language_fragments_match_host_and_stale_fragments_are_removed(self):
        directory = self.directory / 'makepkg.conf.d.host'
        directory.mkdir()
        (directory / 'obsolete.conf').write_text('RUSTFLAGS="wrong"\n')
        self.successful(self.launch())
        host = {p.name: p.read_bytes() for p in Path('/etc/makepkg.conf.d').glob('*.conf')}
        copied = {p.name: p.read_bytes() for p in directory.glob('*.conf')}
        self.assertEqual(copied, host)

    def test_user_makepkg_configuration_is_copied(self):
        directory = self.directory / 'user-config/pacman'
        directory.mkdir(parents=True)
        (directory / 'makepkg.conf').write_text('MAKEFLAGS="-j3"\nCFLAGS="custom"\n')
        self.env['XDG_CONFIG_HOME'] = str(directory.parent)
        self.successful(self.launch())
        self.assertEqual((self.directory / 'makepkg.conf.user.host').read_bytes(),
                         (directory / 'makepkg.conf').read_bytes())

    def test_user_configuration_precedence_and_removal(self):
        self.env['XDG_CONFIG_HOME'] = str(self.directory / 'config')
        current = self.directory / 'config/pacman/makepkg.conf'
        current.parent.mkdir(parents=True)
        current.write_text('MAKEFLAGS="-j3"\n')
        snapshot = self.directory / 'makepkg.conf.user.host'
        self.successful(self.launch())
        self.assertEqual(snapshot.read_bytes(), current.read_bytes())
        current.unlink()
        self.successful(self.launch())
        legacy = Path.home() / '.makepkg.conf'
        self.assertEqual(snapshot.read_bytes(), legacy.read_bytes() if legacy.is_file() else b'')

    def test_configuration_destination_symlinks_are_rejected(self):
        for name in ('pacman.conf.host', 'makepkg.conf.host', 'mirrorlist.host',
                     'makepkg.conf.user.host', 'makepkg.conf.d.host'):
            with self.subTest(name=name):
                path = self.directory / name
                if path.exists():
                    shutil.rmtree(path) if path.is_dir() else path.unlink()
                path.symlink_to(self.directory / 'outside')
                self.failed(self.launch(), 'symlinked')
                path.unlink()

    def test_rootless_podman_mapping(self):
        podman = self.engine.with_name('podman')
        shutil.copy2(self.engine, podman)
        self.env['CONTAINER_ENGINE'] = str(podman)
        self.successful(self.launch())
        self.assertIn('--userns=keep-id', self.calls()[-1])

    def test_engine_and_image_failure_stop_launch(self):
        for mode in ('info-fail', 'build-fail'):
            with self.subTest(mode=mode):
                self.env['TEST_MODE'] = mode
                self.assertNotEqual(self.launch('build', 'alpha').returncode, 0)
                self.assertNotIn('run', [c[0] for c in self.calls()])

    def test_launch_failure_propagates(self):
        self.env['TEST_MODE'] = 'run-fail'
        self.assertEqual(self.launch('build', 'alpha').returncode, 37)

    def test_workspace_lock_blocks_build_and_clean(self):
        with (self.directory / '.aurbuilder.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.failed(self.launch('build', 'alpha'), 'workspace is in use')
            self.failed(self.launch('clean', '-y'), 'workspace is in use')
        self.assertNotIn('build', [c[0] for c in self.calls()])

    def test_symlink_state_is_rejected(self):
        for path in ('cache', 'build', '.aurbuilder.lock', 'cache/gnupg', 'cache/makepkg'):
            with self.subTest(path=path):
                target = self.directory / path
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    shutil.rmtree(target) if target.is_dir() else target.unlink()
                target.symlink_to(self.directory / 'elsewhere')
                self.failed(self.launch('build', 'alpha'), 'symlinked')
                target.unlink()

    def test_gnupg_permissions_repaired(self):
        keyring = self.directory / 'cache/gnupg'
        keyring.mkdir(parents=True, mode=0o755)
        self.successful(self.launch('build', 'alpha'))
        self.assertEqual(keyring.stat().st_mode & 0o777, 0o700)

    def test_clean_target_keeps_other_cache_and_outputs(self):
        for name in ('cache/yay/alpha/file', 'cache/yay/beta/file', 'cache/gnupg/key', 'build/result'):
            path = self.directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('keep')
        self.successful(self.launch('clean', '--pkg', 'alpha', '-y'))
        self.assertFalse((self.directory / 'cache/yay/alpha/file').exists())
        for name in ('cache/yay/beta/file', 'cache/gnupg/key', 'build/result'):
            self.assertTrue((self.directory / name).exists())
        self.assertFalse(self.calls())

    def test_clean_refusal_and_eof_keep_cache(self):
        path = self.directory / 'cache/keep'
        path.parent.mkdir()
        path.write_text('keep')
        for response in ('n\n', ''):
            self.successful(self.launch('clean', input=response))
            self.assertTrue(path.exists())

    def test_clean_does_not_follow_child_symlink(self):
        outside = self.directory / 'outside'
        outside.mkdir()
        (outside / 'keep').write_text('keep')
        cache = self.directory / 'cache'
        cache.mkdir()
        (cache / 'link').symlink_to(outside)
        self.successful(self.launch('clean', '-y'))
        self.assertTrue((outside / 'keep').exists())
        self.assertFalse(list(cache.iterdir()))

    def test_clean_image_calls_engine_only_when_requested(self):
        self.successful(self.launch('clean', '-y', '--image'))
        self.assertIn(['image', 'rm', 'aurbuilder-offline-test'], self.calls())

    def test_container_helpers_refuse_host(self):
        for script in ('build.sh', 'entrypoint.sh'):
            with self.subTest(script=script):
                self.assertNotEqual(self.run_command(['bash', str(ROOT / script), 'alpha']).returncode, 0)


class Retry(Fixture):
    def setUp(self):
        super().setUp()
        self.env['PATH'] = str(self.directory / 'bin') + ':' + os.environ['PATH']
        executable(self.directory / 'bin/sleep', '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$TEST_STATE/delays"\n')
        executable(self.directory / 'bin/flaky', '''#!/usr/bin/env bash
n=0
[[ ! -f "$TEST_STATE/count" ]] || read -r n < "$TEST_STATE/count"
printf '%s\\n' "$((n + 1))" > "$TEST_STATE/count"
(( n >= 2 )) && [[ "${TEST_MODE:-}" != fail ]] && exit 0
exit 37
''')

    def test_recovers_and_uses_bounded_backoff(self):
        self.successful(self.run_command(['bash', str(ROOT / 'retry.sh'), '3', 'flaky']))
        self.assertEqual((self.directory / 'count').read_text().strip(), '3')
        self.assertEqual((self.directory / 'delays').read_text().splitlines(), ['5', '10'])

    def test_exhaustion_preserves_exit_status(self):
        self.env['TEST_MODE'] = 'fail'
        result = self.run_command(['bash', str(ROOT / 'retry.sh'), '2', 'flaky'])
        self.assertEqual(result.returncode, 37)
        self.assertEqual((self.directory / 'count').read_text().strip(), '2')

    def test_invalid_attempt_counts(self):
        for count in ('0', '-1', 'abc', '01'):
            self.assertEqual(self.run_command(['bash', str(ROOT / 'retry.sh'), count, 'true']).returncode, 2)

    def test_cancellation_and_missing_command_are_not_retried(self):
        for status in (126, 127, 130, 143):
            with self.subTest(status=status):
                result = self.run_command(['bash', str(ROOT / 'retry.sh'), '3', 'bash', '-c', f'exit {status}'])
                self.assertEqual(result.returncode, status)
        self.assertFalse((self.directory / 'delays').exists())


class ContainerFixture(Fixture):
    def setUp(self):
        super().setUp()
        for name in ('cache', 'build'):
            (self.directory / name).mkdir()

    def container(self, *command, options=()):
        args = [ENGINE, 'run', '--rm', '--init', '--cap-drop=NET_RAW', '--pids-limit', '4096',
                '--security-opt', 'no-new-privileges=false',
                '-v', f'{self.directory}/cache:/cache', '-v', f'{self.directory}/build:/build',
                '-v', f'{self.directory}:/fixture', '-w', '/fixture', *options, IMAGE, *command]
        return self.run_command(args)


class Container(ContainerFixture):
    def test_corrupt_keyring_fails_clearly(self):
        path = self.directory / 'cache/gnupg'
        path.mkdir(mode=0o700)
        (path / 'pubring.kbx').write_bytes(b'invalid-keyring\n' * 100)
        self.failed(self.container('true'), 'cannot initialize the source-verification keyring')

    def test_dirmngr_startup_failure_is_reported(self):
        executable(self.directory / 'bin/gpgconf', '#!/usr/bin/env bash\nexit 127\n')
        self.failed(self.container('true', options=['-e', 'PATH=/fixture/bin:/usr/local/bin:/usr/bin']),
                    'cannot start dirmngr')

    def test_fresh_and_reused_keyring(self):
        for _ in range(2):
            self.successful(self.container('bash', '-c', 'test -s "$GNUPGHOME/pubring.kbx" && gpgconf --launch dirmngr'))
        self.assertEqual((self.directory / 'cache/gnupg').stat().st_mode & 0o777, 0o700)
        self.assertTrue((self.directory / 'cache/logs/makepkg').is_dir())

    def test_keyring_permission_repair(self):
        path = self.directory / 'cache/gnupg'
        path.mkdir(mode=0o755)
        self.successful(self.container('true'))
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_root_and_missing_container_marker_rejected(self):
        self.failed(self.container('true', options=['--user', '0']), 'non-root builder')
        self.failed(self.container('true', options=['-e', 'AUR_BUILDER_CONTAINER=']), 'inside the builder container')

    def test_symlink_keyring_rejected(self):
        (self.directory / 'cache/gnupg').symlink_to('/build')
        self.failed(self.container('true'), 'symlinked state directory')
        self.assertFalse(list((self.directory / 'build').iterdir()))

    def test_file_instead_of_directory_rejected(self):
        (self.directory / 'cache/gnupg').write_text('preserve')
        self.failed(self.container('true'), 'cannot create')
        self.assertEqual((self.directory / 'cache/gnupg').read_text(), 'preserve')

    def test_read_only_state_rejected(self):
        path = self.directory / 'cache'
        path.chmod(0o555)
        try:
            self.failed(self.container('true'), 'owned and writable')
        finally:
            path.chmod(0o755)

    def test_wrong_owner_rejected(self):
        self.successful(self.container('chown', '0:0', '/cache', options=['--user', '0', '--entrypoint', '/usr/bin/env']))
        try:
            self.failed(self.container('true'), 'owned and writable')
        finally:
            self.successful(self.container('chown', f'{os.getuid()}:{os.getgid()}', '/cache',
                                            options=['--user', '0', '--entrypoint', '/usr/bin/env']))

    def test_signed_sources_reject_missing_keys_and_tampering(self):
        self.successful(self.container('bash', '-euo', 'pipefail', '-c', r'''
export LC_ALL=C
mkdir -m 700 /fixture/signer
gpg --homedir /fixture/signer --batch --pinentry-mode loopback --passphrase '' \
  --quick-generate-key 'Builder test <builder-test@example.invalid>' ed25519 sign 1d
fingerprint="$(gpg --homedir /fixture/signer --with-colons --list-keys | awk -F: '$1 == "fpr" { print $10; exit }')"
printf 'disposable source\n' > payload
gpg --homedir /fixture/signer --batch --armor --detach-sign payload
gpg --homedir /fixture/signer --export "$fingerprint" > test-key.gpg
cat > PKGBUILD <<'RECIPE'
pkgname=aurbuilder-signature-probe
pkgver=1
pkgrel=1
pkgdesc='Disposable source verification test'
arch=('any')
license=('MIT')
source=('payload' 'payload.asc')
sha256sums=('SOURCE_HASH' 'SKIP')
validpgpkeys=('SOURCE_KEY')
package() {
  install -Dm644 payload "$pkgdir/usr/share/aurbuilder-signature-probe/data"
}
RECIPE
checksum="$(sha256sum payload)"
sed -i "s/SOURCE_HASH/${checksum%% *}/; s/SOURCE_KEY/$fingerprint/" PKGBUILD
if makepkg --verifysource --force > missing-key.log 2>&1; then
  echo 'ERROR: source signature accepted without its key' >&2; exit 1
fi
grep -Fq 'unknown public key' missing-key.log
gpg --batch --import test-key.gpg
makepkg --noconfirm --force
cp payload payload.original
printf tampered >> payload
if makepkg --verifysource --force > tampered-source.log 2>&1; then
  echo 'ERROR: tampered source accepted' >&2; exit 1
fi
grep -Fq 'did not pass the validity check' tampered-source.log
mv payload.original payload
cp payload.asc payload.asc.original
printf invalid-signature > payload.asc
if makepkg --verifysource --force > tampered-signature.log 2>&1; then
  echo 'ERROR: tampered signature accepted' >&2; exit 1
fi
grep -Eq 'could not be verified|did not pass' tampered-signature.log
mv payload.asc.original payload.asc
makepkg --verifysource --force
'''))

    def test_container_paths_and_scoped_sudo(self):
        command = '''
set -e
[[ "$(pacman-conf CacheDir)" == /cache/pacman/ ]]
source /usr/share/makepkg/util/config.sh
source_makepkg_config
[[ "$PKGDEST" == /build && "$SRCDEST" == /cache/makepkg/srcdest ]]
[[ "${PACMAN_AUTH[*]}" == sudo ]]
sudo -n pacman --version >/dev/null
! sudo -n /usr/bin/true 2>/dev/null
'''
        self.successful(self.container('bash', '-c', command))

    def test_real_makepkg_build_uses_host_flags_and_packaging_policy(self):
        (self.directory / 'PKGBUILD').write_text(r'''pkgname=aurbuilder-config-probe
pkgver=1
pkgrel=1
pkgdesc='Disposable host configuration test'
arch=('x86_64')
license=('MIT')
build() {
  printf 'int main(void) { return 0; }\n' > probe.c
  printf '%s\0' "$CFLAGS" "${RUSTFLAGS:-}" "${OPTIONS[*]}" \
    "${BUILDENV[*]}" "${COMPRESSZST[*]}" > /fixture/actual-config
  set -x
  cc $CFLAGS $LDFLAGS probe.c -o probe
  set +x
}
package() {
  install -Dm755 probe "$pkgdir/usr/bin/aurbuilder-config-probe"
  install -Dm644 probe.c "$pkgdir/usr/share/aurbuilder-config-probe/purged.txt"
}
''')
        result = self.container('bash', '-ec', r'''
source /usr/share/makepkg/util/config.sh
source_makepkg_config
printf '%s\0' "$CFLAGS" "${RUSTFLAGS:-}" "${OPTIONS[*]}" \
  "${BUILDENV[*]}" "${COMPRESSZST[*]}" "${PURGE_TARGETS[*]}" > /fixture/expected-config
makepkg --noconfirm --force
while IFS= read -r archive; do
  [[ "$(pacman -Qp -- "$archive")" == 'aurbuilder-config-probe 1-1' ]] || continue
  bsdtar -xOf "$archive" .BUILDINFO > /fixture/buildinfo
  bsdtar -tf "$archive" > /fixture/contents
  bsdtar -xOf "$archive" usr/bin/aurbuilder-config-probe > /fixture/probe
done < <(makepkg --packagelist)
chmod +x /fixture/probe
/fixture/probe
''')
        self.successful(result)
        expected = (self.directory / 'expected-config').read_text().split('\0')
        actual = (self.directory / 'actual-config').read_text().split('\0')
        options = expected[2].split()
        if '!buildflags' not in options:
            for index in (0, 1):
                self.assertIn(expected[index].strip(), actual[index])
        self.assertEqual(actual[2:5], expected[2:5])
        info = (self.directory / 'buildinfo').read_text().splitlines()
        self.assertEqual([line.removeprefix('options = ') for line in info if line.startswith('options = ')], options)
        self.assertEqual([line.removeprefix('buildenv = ') for line in info if line.startswith('buildenv = ')], expected[3].split())
        if 'purge' in options and '*.txt' in expected[5].split():
            self.assertNotIn('/purged.txt', (self.directory / 'contents').read_text())
        if '!debug' in options:
            self.assertFalse(list((self.directory / 'build').glob('*-debug-*.pkg.tar.*')))


class Build(ContainerFixture):
    def setUp(self):
        super().setUp()
        executable(self.directory / 'bin/sudo', '''#!/usr/bin/env bash
printf 'sudo %s\\n' "$*" >> /cache/commands
if [[ " $* " == *' -Syu '* && "${TEST_MODE:-}" == upgrade-fail ]]; then exit 37; fi
exit 0
''')
        executable(self.directory / 'bin/retry', '#!/usr/bin/env bash\nshift\nexec "$@"\n')
        executable(self.directory / 'bin/pacman', '''#!/usr/bin/env bash
case "$1" in
  -Q) cat "/cache/installed/$3" ;;
  -Qp) cat -- "$3" ;;
  *) exit 91 ;;
esac
''')
        executable(self.directory / 'bin/yay', '''#!/usr/bin/env bash
printf 'yay %s\\n' "$*" >> /cache/commands
case "${TEST_MODE:-}" in
  checksum) echo '==> ERROR: One or more files did not pass the validity check!'; exit 43 ;;
  pgp) echo ' -> problem importing keys'; exit 17 ;;
  build-fail) echo 'compiler failed'; exit 19 ;;
esac
if [[ "${TEST_MODE:-}" == pgp-* && ( ! -f /cache/key-imported || "$TEST_MODE" == pgp-still-failing ) ]]; then
  key=0123456789ABCDEF0123456789ABCDEF01234567
  [[ "$TEST_MODE" != pgp-short ]] || key=01234567
  [[ "$TEST_MODE" != pgp-64 ]] || key=0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF
  if [[ "$TEST_MODE" == pgp-colored ]]; then
    printf '\\033[1;33m -> \\033[0m\\033[1m%s\\033[0m, required by: alpha\\n' "$key"
  else
    printf ' -> %s, required by: alpha\\n' "$key"
    printf ' -> %s, required by: beta\\n' "$key"
  fi
  echo ' -> problem importing keys'
  exit 17
fi
mkdir -p /cache/installed
for pkg in "$@"; do
  case "$pkg" in alpha|beta|alpha-doc|foo+bar) ;; *) continue ;; esac
  if [[ "${TEST_MODE:-}" == install-fail ]]; then
    printf '%s 1-1\\n' "$pkg" > "/build/$pkg-1-1-any.pkg.tar.zst"
    echo 'failed to commit transaction (conflicting files)'
    exit 23
  fi
  [[ "${TEST_MODE:-}" != false-success ]] || continue
  printf '%s 1-1\\n' "$pkg" > "/cache/installed/$pkg"
  if [[ "${TEST_MODE:-}" == still-missing ]]; then continue; fi
  if [[ "${TEST_MODE:-}" == missing && " $* " != *' --rebuild '* ]]; then continue; fi
  if [[ "${TEST_MODE:-}" == stale && " $* " != *' --rebuild '* ]]; then
    printf '%s 0-1\\n' "$pkg" > "/build/$pkg-0-1-any.pkg.tar.zst"
    continue
  fi
  printf '%s 1-1\\n' "$pkg" > "/build/$pkg-1-1-any.pkg.tar.zst"
done
''')
        executable(self.directory / 'bin/gpg', '''#!/usr/bin/env bash
if [[ " $* " != *' --recv-keys '* ]]; then exec /usr/bin/gpg "$@"; fi
printf 'gpg %s\\n' "$*" >> /cache/commands
[[ "${TEST_MODE:-}" != pgp-unavailable && " $* " == *' hkps://keys.openpgp.org '* ]] || exit 2
touch /cache/key-imported
''')
        executable(self.directory / 'bin/tee', '''#!/usr/bin/env bash
if [[ "${TEST_MODE:-}" == log-fail && "$1" == -a ]]; then cat >/dev/null; exit 28; fi
exec /usr/bin/tee "$@"
''')

    def build(self, *targets, mode='normal', shell=None):
        command = ['bash', '-c', shell] if shell else ['build', *targets]
        return self.container(*command, options=['-e', 'PATH=/fixture/bin:/usr/local/bin:/usr/bin',
                                                '-e', 'TEST_MODE=' + mode])

    def calls(self):
        path = self.directory / 'cache/commands'
        return path.read_text().splitlines() if path.exists() else []

    def test_multiple_targets_use_one_resolution(self):
        result = self.build('alpha', 'beta', 'alpha-doc', 'foo+bar')
        self.successful(result)
        self.assertEqual(len([c for c in self.calls() if c.startswith('yay')]), 1)
        self.assertIn('/build/alpha-doc-1-1-any.pkg.tar.zst', result.stdout)
        self.assertIn('sudo -n pacman -Syu --noconfirm', self.calls())

    def test_duplicate_targets_print_unique_outputs(self):
        result = self.build('alpha', 'alpha')
        self.successful(result)
        self.assertEqual(result.stdout.count('/build/alpha-1-1-any.pkg.tar.zst'), 1)

    def test_uncompressed_archive_is_accepted(self):
        path = self.directory / 'build/alpha-1-1-any.pkg.tar'
        path.write_text('alpha 1-1\n')
        result = self.build('alpha', mode='still-missing')
        self.successful(result)
        self.assertIn('/build/alpha-1-1-any.pkg.tar', result.stdout)
        self.assertNotIn('rebuilding', result.stdout)

    def test_missing_and_stale_outputs_rebuild_once(self):
        for mode in ('missing', 'stale'):
            with self.subTest(mode=mode):
                (self.directory / 'cache/commands').unlink(missing_ok=True)
                for path in (self.directory / 'build').iterdir():
                    path.unlink()
                result = self.build('alpha', mode=mode)
                self.successful(result)
                yay = [c for c in self.calls() if c.startswith('yay')]
                self.assertEqual(len(yay), 2)
                self.assertIn('--rebuild alpha', yay[-1])
                self.assertNotIn('--needed', yay[-1])

    def test_signature_symlink_corrupt_wrong_package_outputs_do_not_count(self):
        out = self.directory / 'build'
        for name, data in [('alpha-1-1-any.pkg.tar.zst.sig', 'alpha 1-1\n'),
                           ('alpha-1-1-any.pkg.tar.xz', 'not package metadata'),
                           ('alpha-doc-1-1-any.pkg.tar.zst', 'alpha-doc 1-1\n')]:
            (out / name).write_text(data)
        (out / 'alpha-1-1-any.pkg.tar.zst').symlink_to('/build/alpha-1-1-any.pkg.tar.zst.sig')
        self.failed(self.build('alpha', mode='still-missing'), 'no package archive matching')

    def test_upgrade_failure_stops_before_yay(self):
        self.failed(self.build('alpha', mode='upgrade-fail'), 'container upgrade failed')
        self.assertFalse([c for c in self.calls() if c.startswith('yay')])

    def test_failures_never_report_success(self):
        for mode, message in [('checksum', 'source verification failed'), ('pgp', 'source key'),
                              ('build-fail', 'build failed'), ('install-fail', 'build failed'),
                              ('false-success', 'no package archive matching')]:
            with self.subTest(mode=mode):
                result = self.build('alpha', mode=mode)
                self.failed(result, message)
                self.assertNotIn('done --', result.stdout)

    def test_log_write_failure_stops_build(self):
        self.failed(self.build('alpha', mode='log-fail'), 'cannot write build log')

    def test_pgp_fallback_imports_unique_full_fingerprints_and_retries_once(self):
        for mode in ('pgp-import', 'pgp-colored', 'pgp-64'):
            with self.subTest(mode=mode):
                (self.directory / 'cache/commands').unlink(missing_ok=True)
                (self.directory / 'cache/key-imported').unlink(missing_ok=True)
                self.successful(self.build('alpha', mode=mode))
                calls = self.calls()
                self.assertEqual(len([c for c in calls if c.startswith('yay')]), 2)
                imports = [c for c in calls if c.startswith('gpg')]
                self.assertEqual(len(imports), 2)
                self.assertIn('hkps://keyserver.ubuntu.com', imports[0])
                self.assertIn('hkps://keys.openpgp.org', imports[1])
                self.assertIn(len(imports[1].split()[-1]), (40, 64))

    def test_short_key_ids_are_never_imported(self):
        self.failed(self.build('alpha', mode='pgp-short'), 'could not import')
        self.assertFalse([c for c in self.calls() if c.startswith('gpg')])

    def test_pgp_server_and_retry_failures_are_bounded(self):
        for mode, yay_count in [('pgp-unavailable', 1), ('pgp-still-failing', 2)]:
            with self.subTest(mode=mode):
                (self.directory / 'cache/commands').unlink(missing_ok=True)
                (self.directory / 'cache/key-imported').unlink(missing_ok=True)
                self.failed(self.build('alpha', mode=mode), 'ERROR')
                self.assertEqual(len([c for c in self.calls() if c.startswith('yay')]), yay_count)
                self.assertEqual(len([c for c in self.calls() if c.startswith('gpg')]), 2)

    def test_invalid_targets_do_not_upgrade(self):
        for target in ('--overwrite', '../alpha', 'alpha beta'):
            self.failed(self.build(target), 'invalid package name')
        self.assertFalse(self.calls())

    def test_build_lock_blocks_concurrent_transaction(self):
        command = 'exec 9>>/cache/locks/build.lock; flock -n 9; build alpha'
        self.failed(self.build(shell=command), 'another build is using this cache')
        self.assertFalse(self.calls())

    def test_build_lock_symlink_rejected(self):
        path = self.directory / 'cache/locks'
        path.mkdir()
        (path / 'build.lock').symlink_to('/build/keep')
        self.failed(self.build('alpha'), 'symlinked build lock')
        self.assertFalse((self.directory / 'build/keep').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
