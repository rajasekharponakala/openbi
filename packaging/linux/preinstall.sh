#!/bin/sh
set -e
if ! getent group openbi >/dev/null; then
  groupadd --system openbi
fi
if ! getent passwd openbi >/dev/null; then
  useradd --system --gid openbi --home-dir /var/lib/openbi --no-create-home --shell /usr/sbin/nologin openbi
fi
