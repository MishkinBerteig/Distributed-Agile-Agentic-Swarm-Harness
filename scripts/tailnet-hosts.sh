#!/usr/bin/env bash
# Print a DAASH_DISCOVERY_HOSTS line for .env from your tailnet (docs/DESIGN-0.4.md section 5.8).
#
#   scripts/tailnet-hosts.sh            # DAASH_DISCOVERY_HOSTS=jane-mac.example.ts.net,...
#   scripts/tailnet-hosts.sh --all      # also peers that are offline right now
#
# Run it on the machine where DAASH runs, with the Tailscale CLI installed and logged in. It reads
# `tailscale status --json` and prints the MagicDNS names of this machine's peers (online ones
# only, unless --all). It prints names only: no keys, addresses or other secrets.
#
# Why: from Docker Desktop on a Mac, DAASH's containers usually cannot reach other machines by
# their LAN addresses, but they can by their Tailscale names, so naming the machines is what
# makes discovery work there. Put the printed line into .env, then `docker compose up -d`.
set -euo pipefail

include_offline=false
if [ "${1:-}" = "--all" ]; then
  include_offline=true
elif [ -n "${1:-}" ]; then
  echo "usage: $0 [--all]" >&2
  exit 2
fi

TS="${TAILSCALE:-tailscale}"
if ! command -v "$TS" >/dev/null 2>&1; then
  for candidate in /Applications/Tailscale.app/Contents/MacOS/Tailscale /usr/local/bin/tailscale \
                   /opt/homebrew/bin/tailscale; do
    if [ -x "$candidate" ]; then TS="$candidate"; break; fi
  done
fi
if ! command -v "$TS" >/dev/null 2>&1 && [ ! -x "$TS" ]; then
  echo "tailscale CLI not found (set TAILSCALE=/path/to/tailscale)" >&2
  exit 1
fi

"$TS" status --json | python3 -c '
import json, sys
include_offline = sys.argv[1] == "true"
status = json.load(sys.stdin)
names = []
for peer in (status.get("Peer") or {}).values():
    if not include_offline and not peer.get("Online"):
        continue
    name = (peer.get("DNSName") or "").rstrip(".")
    if name and name not in names:
        names.append(name)
if not names:
    sys.stderr.write("no " + ("" if include_offline else "online ") + "peers found\n")
    sys.exit(1)
print("DAASH_DISCOVERY_HOSTS=" + ",".join(sorted(names)))
' "$include_offline"
