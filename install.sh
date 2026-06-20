#!/bin/bash
set -e
OS=$(uname -s)
if [ "$OS" = "Darwin" ]; then
  BINARY="datapro-agent-macos-latest"
else
  BINARY="datapro-agent-ubuntu-latest"
fi
URL="https://github.com/ManVats0/TheGod-sToolPython/releases/latest/download/$BINARY"
curl -sSL -o datapro-agent "$URL"
chmod +x datapro-agent
echo "Starting DataPro Agent..."
./datapro-agent
