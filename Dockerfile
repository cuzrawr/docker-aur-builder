FROM archlinux:base-devel

ARG UID=1000
ARG GID=1000
ENV GNUPGHOME=/cache/gnupg
ENV AUR_BUILDER_CONTAINER=1

# Retry helper for flaky network (pacman mirrors, git clone).
COPY retry.sh /usr/local/bin/retry
RUN chmod 755 /usr/local/bin/retry

# /cache/pacman must exist before any bind-mount (image build stage).
RUN mkdir -p /cache/pacman

# Host mirrorlist if present (used for all pacman traffic in image + runtime).
COPY mirrorlist.host /tmp/mirrorlist.host
RUN if [ -s /tmp/mirrorlist.host ]; then cp /tmp/mirrorlist.host /etc/pacman.d/mirrorlist; fi \
    && rm -f /tmp/mirrorlist.host

# Host pacman.conf if present; force CacheDir to persistent /cache.
# Host IgnorePkg/IgnoreGroup are host runtime policy and break dependency
# resolution for packages that need ignored dependencies.
# Everything else (repos, mirrors, Color, ParallelDownloads, SigLevel,
# HoldPkg, NoExtract, multilib, etc.) is left intact.
COPY pacman.conf.host /tmp/pacman.conf.host
RUN if [ -s /tmp/pacman.conf.host ]; then cp /tmp/pacman.conf.host /etc/pacman.conf; fi \
    && sed -i \
         -e '/^[[:space:]]*CacheDir[[:space:]]*=/d' \
         -e '/^[[:space:]]*IgnorePkg[[:space:]]*=/d' \
         -e '/^[[:space:]]*IgnoreGroup[[:space:]]*=/d' \
         /etc/pacman.conf \
    && printf '\n[options]\nCacheDir = /cache/pacman/\n' >> /etc/pacman.conf \
    && rm -f /tmp/pacman.conf.host

# Host makepkg.conf if present; force container paths + PACMAN_AUTH=sudo.
# Preserve the complete host configuration, including its language fragments.
# Host PKGDEST/SRCDEST would be invalid inside the container.
# Package transactions use the container's scoped sudo configuration below.
COPY makepkg.conf.host /tmp/makepkg.conf.host
COPY makepkg.conf.d.host/ /tmp/makepkg.conf.d.host/
COPY makepkg-container.conf /etc/aurbuilder-makepkg.conf
RUN if [ -s /tmp/makepkg.conf.host ]; then \
         cp /tmp/makepkg.conf.host /etc/makepkg.conf; \
         rm -rf /etc/makepkg.conf.d; \
         cp -r /tmp/makepkg.conf.d.host /etc/makepkg.conf.d; \
       fi \
    && printf '\nsource /etc/aurbuilder-makepkg.conf\n' >> /etc/makepkg.conf \
    && rm -rf /tmp/makepkg.conf.host /tmp/makepkg.conf.d.host

# Base tools. sudo is required for non-root pacman calls from makepkg/yay.
RUN retry 5 pacman -Syu --noconfirm --needed \
      git sudo gnupg ccache distcc debugedit zstd xz bzip2 \
    && rm -rf /var/cache/pacman/pkg/*

# Non-root builder matching host UID/GID (bind mounts stay owned by host user).
# makepkg refuses root; pacman uses scoped sudoers, no password, no tty.
COPY makepkg.conf.user.host /tmp/makepkg.conf.user.host
RUN (getent group "$GID" >/dev/null || groupadd -g "$GID" builder) \
    && useradd -m -u "$UID" -g "$GID" -s /bin/bash builder \
    && { \
         echo 'Defaults:builder !requiretty'; \
         echo 'builder ALL=(ALL) NOPASSWD: /usr/bin/pacman, /usr/bin/pacman-key'; \
       } > /etc/sudoers.d/builder \
    && chmod 440 /etc/sudoers.d/builder \
    && visudo -cf /etc/sudoers.d/builder \
    && mkdir -p /build /cache/makepkg/srcdest /cache/logs/makepkg /cache/ccache /cache/yay /cache/gnupg \
    && chmod 700 /cache/gnupg \
    && mkdir -p /home/builder/.config/pacman \
    && cp /tmp/makepkg.conf.user.host /home/builder/.config/pacman/makepkg.conf \
    && printf '\nsource /etc/aurbuilder-makepkg.conf\n' >> /home/builder/.config/pacman/makepkg.conf \
    && rm /tmp/makepkg.conf.user.host \
    && chown -R "$UID:$GID" /build /cache /home/builder

# Install yay-bin as the non-root builder (makepkg forbids root; --asroot removed).
# sudoers already allows the needed pacman calls.
RUN retry 5 sh -c 'rm -rf /tmp/yay-bin && git clone --quiet https://aur.archlinux.org/yay-bin.git /tmp/yay-bin' \
    && chown -R "$UID:$GID" /tmp/yay-bin \
    && su - builder -c 'cd /tmp/yay-bin && makepkg -si --noconfirm --needed' \
    && rm -rf /tmp/yay-bin

# Build helper used by both `./run.sh build <pkg>` and interactive shell.
COPY build.sh /usr/local/bin/build
COPY entrypoint.sh /usr/local/bin/aurbuilder-entrypoint
RUN chmod 755 /usr/local/bin/build /usr/local/bin/aurbuilder-entrypoint

USER builder
WORKDIR /home/builder

# Non-interactive yay defaults + persistent AUR build dir under /cache.
COPY --chown=${UID}:${GID} yay-config.json .config/yay/config.json

WORKDIR /build
ENTRYPOINT ["/usr/local/bin/aurbuilder-entrypoint"]
CMD ["bash", "-l"]
