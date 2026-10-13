# Docker AUR builder

- Target: host Arch configuration and CPU.
- Requirements: Linux x86-64, Bash, `flock`, Docker or Podman; normal user with engine access.
- Host packages: unchanged by builds.

## Usage

```sh
./run.sh build package-name  # Packages: build/; logs: cache/logs/
./run.sh                     # Shell: build, yay, makepkg
./run.sh clean --pkg package-name  # Clear one AUR checkout
./run.sh clean               # Clear cache after confirmation; retain build/
./run.sh clean -y --image    # Clear cache and image; retain build/
```

- `CONTAINER_ENGINE=docker|podman`: select engine; default prefers Podman.
- `AUR_BUILDER_IMAGE`: image tag; default `aurbuilder`.
- Every launch: refresh host snapshots; rebuild missing/changed image layers.
- Every build: full `pacman -Syu`; joint dependency resolution; verified package outputs.

## Host configuration

| Imported every launch | Preserved |
| --- | --- |
| `/etc/pacman.conf`, `/etc/pacman.d/mirrorlist` | Repositories, mirrors, signature policy, `NoExtract`, display settings |
| `/etc/makepkg.conf`, `/etc/makepkg.conf.d/*.conf` | Compiler/Rust flags, native CPU settings, LTO, build options, stripping, purge rules, compression |
| `${XDG_CONFIG_HOME:-$HOME/.config}/pacman/makepkg.conf`, otherwise `~/.makepkg.conf` | User overrides in makepkg order |

- Container adjustments: cache/output paths under `/cache` and `/build`; `PACMAN_AUTH=(sudo)`.
- Existing dependency policy: remove active `IgnorePkg`/`IgnoreGroup` from the container copy.
- `MAKEFLAGS`: preserve explicit value; use `-j$(nproc)` when unset/empty.
- Missing host config: base-image fallback. Host-only includes/repositories: provide equivalent files inside the image.
- Edit host originals; generated snapshots, caches and packages stay outside Git.

## Recovery

| Issue | Action |
| --- | --- |
| Build failure | Read `cache/logs/`; open `./run.sh` for diagnosis |
| Missing GPG directory | Relaunch; keyring and dirmngr initialize automatically |
| Old container GPG failure | Inside it: `mkdir -p /cache/gnupg && chmod 700 /cache/gnupg`; retry build |
| Missing output | One automatic rebuild; archive metadata must match installed version |
| Pruned image | Launch normally; local cache and outputs survive |
| Lost executable bit | `chmod +x run.sh` |

- Source verification: checksums and signatures enabled; bounded keyserver/network retries.
- Container system-manager hook warnings: expected without systemd.
- Manual `yay`/`makepkg`: one at a time; local recipes under `/build`.
- Fresh base: `docker pull archlinux:base-devel`; multilib compilation: `archlinux:multilib-devel` plus host multilib repository.

## Checks

```sh
python3 tests/test_builder.py Launcher Retry  # Portable; no image needed
./run.sh </dev/null                           # Prepare image and host snapshots
python3 tests/check_host_config.py             # Compare effective Arch host settings
python3 tests/test_builder.py                  # Full regression; disposable test packages
bash tests/integration.sh package-name         # Networked build of a selected AUR package
```

- `AUR_BUILDER_TEST_IMAGE`: existing image for tests; default `aurbuilder`.
- Test state: `<project-directory-name>tmp/`; isolated writable mounts.
- GitHub checks: ShellCheck, Bash syntax, portable tests.
- Container checks: configuration matching, keyring failures, signed sources and tamper rejection.
- Integration checks: selected packages, cached reuse, missing outputs, cleanup and image recreation.
- Untested platform: rootless Podman.

## Limits

- Non-root builder; matching UID/GID; scoped sudo for pacman/pacman-key.
- Writable mounts: `build/`, `cache/`; AUR code can modify both. Keep personal signing keys elsewhere.
- CPU-specific flags target the host CPU; no automatic portability conversion.
- Builder: [MIT](LICENSE). Packages and downloaded sources: their upstream licenses.
