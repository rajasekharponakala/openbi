#!/bin/sh
set -e
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  systemctl stop openbi.service >/dev/null 2>&1 || true
  systemctl disable openbi.service >/dev/null 2>&1 || true
fi
