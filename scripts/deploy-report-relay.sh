#!/bin/sh
# Install or update the report relay (relay/) on a server, over SSH.
#
#   scripts/deploy-report-relay.sh [--dry-run] user@host
#   scripts/deploy-report-relay.sh [--dry-run] --local
#
# It copies two Python files and a systemd unit, creates the configuration
# from the example when there is none, and restarts the service. It never
# touches an existing relay.env, never handles the App's private key (that
# is copied by hand, see docs/report-relay.md), creates no GitHub App and no
# DNS record, and configures no reverse proxy.
#
# The remote user needs sudo. --dry-run prints what would run.
# SPDX-License-Identifier: MIT
set -eu

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
dry=0
target=

for arg in "$@"; do
    case $arg in
        --dry-run) dry=1 ;;
        --local) target=local ;;
        -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "unknown option: $arg" >&2; exit 2 ;;
        *) target=$arg ;;
    esac
done
if [ -z "$target" ]; then
    echo "usage: $0 [--dry-run] user@host | --local" >&2
    exit 2
fi

for file in relay/report_relay.py bol/scrub.py relay/bol-report-relay.service \
        relay/relay.env.example; do
    if [ ! -f "$root/$file" ]; then
        echo "missing $file: run this from a checkout of the repository" >&2
        exit 1
    fi
done

# The relay must be the one the tests ran against.
python3 -c 'import ast, sys
for path in sys.argv[1:]:
    ast.parse(open(path, encoding="utf-8").read(), path)' \
    "$root/relay/report_relay.py" "$root/bol/scrub.py"

run() {
    if [ "$dry" -eq 1 ]; then
        echo "+ $*"
    else
        "$@"
    fi
}

remote() {
    if [ "$target" = local ]; then
        run sh -c "$1"
    else
        run ssh "$target" "$1"
    fi
}

# A fresh private directory on the machine being installed; a dry run only
# needs a name to print.
if [ "$dry" -eq 1 ]; then
    stage=/tmp/bol-report-relay.XXXXXX
elif [ "$target" = local ]; then
    stage=$(mktemp -d /tmp/bol-report-relay.XXXXXX)
else
    stage=$(ssh "$target" 'mktemp -d /tmp/bol-report-relay.XXXXXX')
fi
case $stage in
    /tmp/bol-report-relay.*) ;;
    *) echo "could not create a staging directory on $target" >&2; exit 1 ;;
esac

if [ "$target" = local ]; then
    run cp "$root/relay/report_relay.py" "$root/bol/scrub.py" \
        "$root/relay/bol-report-relay.service" \
        "$root/relay/relay.env.example" "$stage/"
else
    run scp -q "$root/relay/report_relay.py" "$root/bol/scrub.py" \
        "$root/relay/bol-report-relay.service" \
        "$root/relay/relay.env.example" "$target:$stage/"
fi

remote "set -eu
sudo install -d -m 0755 /opt/bol-report-relay
sudo install -d -m 0750 /etc/bol-report-relay
sudo install -m 0644 $stage/report_relay.py $stage/scrub.py /opt/bol-report-relay/
sudo install -m 0644 $stage/bol-report-relay.service /etc/systemd/system/
if ! sudo test -e /etc/bol-report-relay/relay.env; then
    sudo install -m 0640 $stage/relay.env.example /etc/bol-report-relay/relay.env
    echo 'created /etc/bol-report-relay/relay.env: fill in the App ID and the installation ID'
fi
rm -rf $stage
sudo systemctl daemon-reload
if sudo test -e /etc/bol-report-relay/app-key.pem; then
    sudo systemctl enable bol-report-relay.service
    sudo systemctl restart bol-report-relay.service
    sleep 2
    sudo systemctl --no-pager --lines=5 status bol-report-relay.service || true
    if command -v curl >/dev/null 2>&1; then
        curl -fsS http://127.0.0.1:8787/healthz && echo
    fi
else
    echo 'installed, not started: /etc/bol-report-relay/app-key.pem is not there yet'
fi"

echo "Done. Next steps are in docs/report-relay.md."
