#!/bin/sh
set -eu

config=/run/csai-protocol/protocol.env
if [ ! -r "$config" ]; then
  printf '%s\n' 'Required private protocol config is unavailable' >&2
  exit 78
fi
requested_mcp_enabled=${MCP_ENABLED:-}
requested_a2a_enabled=${A2A_AFTERSALES_ENABLED:-}
set -a
# Generated locally from synthetic fixtures; file is mode 0440 and never logged.
. "$config"
set +a
# Explicit operator rollback always wins over synthetic defaults in the file.
if [ -n "$requested_mcp_enabled" ]; then
  MCP_ENABLED=$requested_mcp_enabled
  export MCP_ENABLED
fi
if [ -n "$requested_a2a_enabled" ]; then
  A2A_AFTERSALES_ENABLED=$requested_a2a_enabled
  export A2A_AFTERSALES_ENABLED
fi
exec "$@"
