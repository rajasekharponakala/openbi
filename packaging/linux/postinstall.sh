#!/bin/sh
set -e
chown -R openbi:openbi /var/lib/openbi
chgrp openbi /etc/openbi/openbi.env || true
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  systemctl daemon-reload || true
fi
cat <<MSG
OpenBI installed.
  Create an administrator:  sudo -u openbi OPENBI_HOME=/var/lib/openbi openbi create-user you@example.com --admin
  Start the service:        sudo systemctl enable --now openbi
  Then open http://127.0.0.1:8000 (settings: /etc/openbi/openbi.env)
MSG
