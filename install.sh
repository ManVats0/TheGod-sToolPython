#!/usr/bin/env bash
# The God's Tool: connect this computer in one command.
#   curl -sSL https://api.thegodstool.com/download/install.sh | bash -s -- ABCD-EFGH [server-url]
set -e
CODE="$1"
SERVER="${2:-https://api.thegodstool.com}"
if [ -z "$CODE" ]; then
  echo "Missing pairing code. In the app open Tunneling > One click and copy the command shown there."
  exit 1
fi
DIR="${THEGODSTOOL_HOME:-$HOME/.thegodstool}"
mkdir -p "$DIR"

if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
  echo "Setting up the agent (one time, about a minute)..."
  if [ ! -x "$DIR/venv/bin/python" ]; then
    python3 -m venv "$DIR/venv" || { echo "Could not create a Python environment. On Debian/Ubuntu run: sudo apt install python3-venv"; exit 1; }
  fi
  "$DIR/venv/bin/python" -m pip install -q --disable-pip-version-check websockets aiomysql asyncpg motor aiopg
  curl -fsSL "$SERVER/download/agent.py" -o "$DIR/agent.py"
  printf '#!/usr/bin/env bash\nexec "%s/venv/bin/python" "%s/agent.py" "$@"\n' "$DIR" "$DIR" > "$DIR/start.sh"
  chmod +x "$DIR/start.sh"
  echo "To start it again later, run: $DIR/start.sh"
  exec "$DIR/start.sh" --pair "$CODE" --server "$SERVER"
fi

# No usable Python: use the prebuilt program instead.
case "$(uname -s)" in
  Darwin) BINARY="datapro-agent-macos-latest" ;;
  *)      BINARY="datapro-agent-ubuntu-latest" ;;
esac
curl -fsSL -o "$DIR/datapro-agent" "https://github.com/ManVats0/TheGod-sToolPython/releases/latest/download/$BINARY"
chmod +x "$DIR/datapro-agent"
echo "To start it again later, run: $DIR/datapro-agent"
exec "$DIR/datapro-agent" --pair "$CODE" --server "$SERVER"
