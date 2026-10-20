"""Fixed values the protocol specification defines."""

PROTOCOL_VERSION = 1
DEFAULT_PORT = 5328

# Largest payload a frame may carry. A bigger length header is treated as
# garbage or an attack, and the connection is closed.
MAX_PAYLOAD = 64 * 1024

# A client sends PING this often; either side treats this much silence as a
# lost connection.
PING_INTERVAL_SECONDS = 10.0
IDLE_TIMEOUT_SECONDS = 30.0

# How long the server holds a disconnected player's seat for them to resume.
RESUME_WINDOW_SECONDS = 60.0
