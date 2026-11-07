#!/usr/bin/env bash
# Start a target's dispatcher on the laptop (inside WSL2), detached so it outlives the SSH
# session. WSL keeps running only while a client is attached; the RiftboundWslKeeper
# scheduled task provides one, so check it first when a dispatcher dies with the distro.
#   ssh -F E:/ssh/config laptop "wsl -d Ubuntu-24.04 -u max -- bash ~/agentpipe/pipeline/laptop-run.sh riftbound"
# Subcommands other than start pass through: laptop-run.sh status riftbound, stop riftbound.
set -euo pipefail
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin"
export AGENTPIPE_MACHINE=laptop
cd "$(dirname "$0")"
git -C .. pull -q --ff-only
uv sync -q
if [ "$#" -eq 1 ]; then
    exec uv run agentpipe start "$1" --no-watch
fi
exec uv run agentpipe "$@"
