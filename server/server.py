import asyncio
# pyrefly: ignore [missing-import]
import websockets
import json
import sqlite3
import os
import socket
import sys
import logging
import time
import subprocess
import signal
import re
import shutil

# Project root directory (frozen-aware: works for source runs AND packaged builds)
def _resolve_root_dir():
    # PyInstaller / GitHub Actions builds set sys.frozen. In that case __file__
    # points inside the temporary _MEIPASS extraction dir, so .env / db / logs
    # must instead resolve next to the actual executable, where the user can see
    # and edit them.
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    if "__file__" in globals():
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.getcwd()

ROOT_DIR = _resolve_root_dir()
DB_FILE = os.path.join(ROOT_DIR, "db", "porta.db")
DEFAULT_CHANNELS = ["#lobby", "#dev", "#random"]

# Active connections:
# websocket -> {"username": str, "channel": str, "typing": bool, "connection_type": str}
clients = {}

def load_env():
    """Parse the .env file."""
    env_vars = {}
    paths_to_try = [
        ".env",
        "../.env",
        os.path.join(ROOT_DIR, ".env")
    ]
    for path in paths_to_try:
        if path and os.path.exists(path):
            try:
                with open(path, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            env_vars[k.strip()] = v.strip()
                break
            except Exception:
                pass
    return env_vars

# Load config
ENV = load_env()
CONFIG_STATE = (
    os.getenv("CONFIGURATION_STATE")
    or os.getenv("CONFIGUATION_STATE")
    or ENV.get("CONFIGURATION_STATE")
    or ENV.get("CONFIGUATION_STATE")  # keep the original (misspelled) key working
    or "0"
).strip()
IS_PRODUCTION = (CONFIG_STATE == "1")

# Setup logging
LOG_FILE = os.path.join(ROOT_DIR, "porta.log")
logging.basicConfig(
    filename=LOG_FILE,
    filemode="a",
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

def kill_process_on_port(port):
    """Terminate any process listening on the specified port."""
    try:
        cmd = ["lsof", "-t", f"-i:{port}"]
        output = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
        pids = [int(pid) for pid in output.decode().strip().split("\n") if pid.strip()]
        
        my_pid = os.getpid()
        killed = False
        for pid in pids:
            if pid != my_pid:
                logging.info(f"Lingering process found on port {port}: PID {pid}. Terminating...")
                print(f"--> Lingering server instance found on port {port} (PID {pid}). Freeing port...")
                os.kill(pid, signal.SIGKILL)
                killed = True
                
        if killed:
            # Wait for socket release
            time.sleep(1.0)
            return True
    except Exception:
        # Port already free
        pass
    return False

def show_progress(label, duration=0.8):
    """Render a terminal progress bar."""
    width = 30
    sys.stdout.write(f"{label}: [")
    sys.stdout.flush()
    for i in range(width + 1):
        percent = int((i / width) * 100)
        bar = "█" * i + " " * (width - i)
        sys.stdout.write(f"\r{label}: [{bar}] {percent}%")
        sys.stdout.flush()
        time.sleep(duration / width)
    print()

def print_config_table(local_ip, tunnel_domain):
    """Draw server configuration table."""
    state_str = "Production (1)" if IS_PRODUCTION else "Development (0)"
    tunnel_str = f"wss://{tunnel_domain}" if tunnel_domain else "Disabled (Local only)"
    
    lines = [
        f"State:  {state_str}",
        f"Local:  ws://localhost:8765",
        f"WiFi:   ws://{local_ip}:8765",
        f"Tunnel: {tunnel_str}",
        f"Log:    porta.log",
        f"DB:     {DB_FILE}"
    ]
    
    # Calculate the max length of lines
    max_len = max(len(line) for line in lines)
    # Ensure header fits
    header = "PORTA PRO SERVER CONFIGURATION"
    max_len = max(max_len, len(header))
    
    # Top and bottom borders
    top = "╔" + "═" * (max_len + 4) + "╗"
    mid = "╠" + "═" * (max_len + 4) + "╣"
    bot = "╚" + "═" * (max_len + 4) + "╝"
    
    print("\n" + top)
    print(f"║ {header:^{max_len + 2}} ║")
    print(mid)
    for line in lines:
        print(f"║  {line:<{max_len + 1}} ║")
    print(bot + "\n")

def init_db():
    try:
        os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                channel TEXT,
                sender TEXT,
                recipient TEXT,
                text TEXT,
                is_dm BOOLEAN,
                connection_type TEXT DEFAULT 'local'
            )
        """)
        
        # Check table info for migration
        cursor.execute("PRAGMA table_info(channels)")
        columns = cursor.fetchall()
        pk_count = sum(1 for col in columns if col[5] > 0)
        
        if len(columns) > 0 and pk_count == 1:
            logging.info("Migrating channels table schema to compound primary key (name, connection_type)...")
            cursor.execute("ALTER TABLE channels RENAME TO channels_old")
            cursor.execute("""
                CREATE TABLE channels (
                    name TEXT,
                    creator TEXT,
                    connection_type TEXT DEFAULT 'local',
                    PRIMARY KEY (name, connection_type)
                )
            """)
            cursor.execute("INSERT OR IGNORE INTO channels (name, creator, connection_type) SELECT name, creator, 'local' FROM channels_old")
            cursor.execute("DROP TABLE channels_old")
        else:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    name TEXT,
                    creator TEXT,
                    connection_type TEXT DEFAULT 'local',
                    PRIMARY KEY (name, connection_type)
                )
            """)

        # Migrations for existing database instances
        try:
            cursor.execute("ALTER TABLE messages ADD COLUMN connection_type TEXT DEFAULT 'local'")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE channels ADD COLUMN connection_type TEXT DEFAULT 'local'")
        except sqlite3.OperationalError:
            pass

        # Seed default channels for both connection type scopes if empty
        for c_type in ["local", "public"]:
            cursor.execute("SELECT COUNT(*) FROM channels WHERE connection_type = ?", (c_type,))
            if cursor.fetchone()[0] == 0:
                for ch in DEFAULT_CHANNELS:
                    cursor.execute("INSERT OR IGNORE INTO channels (name, creator, connection_type) VALUES (?, ?, ?)", (ch, "System", c_type))
                conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Database initialization error: {e}", exc_info=True)
        if not IS_PRODUCTION:
            raise e

def save_message(channel, sender, recipient, text, is_dm=False, connection_type='local'):
    """Persist a chat message or direct message in the database inside its isolated network scope."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO messages (channel, sender, recipient, text, is_dm, connection_type)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (channel, sender, recipient, text, is_dm, connection_type))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Failed to save message in database: {e}", exc_info=True)
        if not IS_PRODUCTION:
            raise e

def get_channels(connection_type):
    """Retrieve all available channels for a specific isolated connection type scope."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM channels WHERE connection_type = ?", (connection_type,))
        rows = cursor.fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception as e:
        logging.error(f"Failed to query channels: {e}", exc_info=True)
        return []

def get_channel_creator(channel, connection_type):
    """Retrieve the creator of a specific channel within its isolated connection type scope."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT creator FROM channels WHERE name = ? AND connection_type = ?", (channel, connection_type))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        logging.error(f"Failed to get channel creator: {e}", exc_info=True)
        return None

def add_channel(name, creator, connection_type):
    """Create a new channel inside its isolated connection type scope."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO channels (name, creator, connection_type) VALUES (?, ?, ?)", (name, creator, connection_type))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logging.error(f"Failed to add channel: {e}", exc_info=True)
        return False

def delete_channel(name, connection_type):
    """Remove a channel from its isolated connection type scope."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM channels WHERE name = ? AND connection_type = ?", (name, connection_type))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logging.error(f"Failed to delete channel: {e}", exc_info=True)
        return False

def get_channel_history(channel, connection_type, limit=100):
    """Fetch the latest messages from a public channel within its isolated connection type scope."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT sender, text, timestamp FROM messages 
            WHERE channel = ? AND is_dm = 0 AND connection_type = ?
            ORDER BY id DESC LIMIT ?
        """, (channel, connection_type, limit))
        rows = cursor.fetchall()
        conn.close()
        return [{"type": "chat", "channel": channel, "user": r[0], "text": r[1], "timestamp": r[2]} for r in reversed(rows)]
    except Exception as e:
        logging.error(f"Failed to retrieve channel history: {e}", exc_info=True)
        return []

def get_dm_history(user1, user2, connection_type, limit=100):
    """Fetch history of direct messages between two users within their isolated connection type scope."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT sender, text, timestamp FROM messages 
            WHERE is_dm = 1 AND connection_type = ? AND 
            ((sender = ? AND recipient = ?) OR (sender = ? AND recipient = ?))
            ORDER BY id DESC LIMIT ?
        """, (connection_type, user1, user2, user2, user1, limit))
        rows = cursor.fetchall()
        conn.close()
        return [{"type": "chat", "is_dm": True, "user": r[0], "text": r[1], "timestamp": r[2]} for r in reversed(rows)]
    except Exception as e:
        logging.error(f"Failed to retrieve DM history: {e}", exc_info=True)
        return []

def get_unique_username(base_name, connection_type):
    """Return a unique username by checking existence only within the same isolated connection type."""
    existing = {info["username"].lower() for info in clients.values() if info["connection_type"] == connection_type}
    if base_name.lower() not in existing:
        return base_name
    suffix = 2
    while f"{base_name.lower()}_{suffix}" in existing:
        suffix += 1
    return f"{base_name}_{suffix}"

async def broadcast_user_list(connection_type):
    """Broadcast the user list only to clients connected within the same isolated connection type."""
    targets = [ws for ws, info in clients.items() if info["connection_type"] == connection_type]
    if not targets:
        return
    users_data = []
    for ws in targets:
        info = clients[ws]
        users_data.append({
            "username": info["username"],
            "channel": info["channel"],
            "typing": info["typing"]
        })
    payload = json.dumps({
        "type": "user_list",
        "users": users_data
    })
    try:
        websockets.broadcast(targets, payload)
    except Exception as e:
        logging.error(f"Failed to broadcast user list: {e}", exc_info=True)

async def broadcast_channel_list(connection_type):
    """Broadcast the channel list only to clients connected within the same isolated connection type."""
    targets = [ws for ws, info in clients.items() if info["connection_type"] == connection_type]
    if not targets:
        return
    ch_list = get_channels(connection_type)
    payload = json.dumps({
        "type": "channel_list",
        "channels": ch_list
    })
    try:
        websockets.broadcast(targets, payload)
    except Exception as e:
        logging.error(f"Failed to broadcast channel list: {e}", exc_info=True)

async def broadcast_to_channel(channel, payload, connection_type, exclude_ws=None):
    """Send a message only to clients in the same channel and connection type."""
    targets = [ws for ws, info in clients.items() 
               if info["channel"] == channel and info["connection_type"] == connection_type and ws != exclude_ws]
    if targets:
        try:
            websockets.broadcast(targets, payload)
        except Exception as e:
            logging.error(f"Failed to broadcast message to channel {channel}: {e}", exc_info=True)

async def handle_client_message(websocket, message_str):
    """Process incoming client socket messages for routing, channels, nicks, DMs, or owner controls."""
    try:
        data = json.loads(message_str)
    except json.JSONDecodeError:
        return

    msg_type = data.get("type")
    sender_info = clients[websocket]
    username = sender_info["username"]
    connection_type = sender_info["connection_type"]

    if msg_type == "join":
        old_channel = sender_info["channel"]
        new_channel = data.get("channel", "#lobby")
        available_channels = get_channels(connection_type)
        if new_channel not in available_channels:
            new_channel = "#lobby"
        
        # Update connection info
        sender_info["channel"] = new_channel
        sender_info["typing"] = False
        
        # Notify channels
        await broadcast_to_channel(old_channel, json.dumps({
            "type": "system",
            "text": f"({username}) left the channel."
        }), connection_type)
        await broadcast_to_channel(new_channel, json.dumps({
            "type": "system",
            "text": f"({username}) joined the channel."
        }), connection_type)
        
        # Send confirmation acknowledgement
        await websocket.send(json.dumps({
            "type": "join_ack",
            "channel": new_channel
        }))
        
        await broadcast_user_list(connection_type)
        
        # Send channel history logs
        history = get_channel_history(new_channel, connection_type)
        for msg in history:
            await websocket.send(json.dumps(msg))

    elif msg_type == "chat":
        text = data.get("text", "").strip()
        channel = sender_info["channel"]
        if not text:
            return

        save_message(channel, username, None, text, is_dm=False, connection_type=connection_type)
        payload = json.dumps({
            "type": "chat",
            "channel": channel,
            "user": username,
            "text": text
        })
        await broadcast_to_channel(channel, payload, connection_type)

    elif msg_type == "dm":
        recipient = data.get("recipient", "").strip()
        text = data.get("text", "").strip()
        if not recipient or not text:
            return

        recipient_ws = None
        for ws, info in clients.items():
            if info["username"].lower() == recipient.lower() and info["connection_type"] == connection_type:
                recipient_ws = ws
                break

        if recipient_ws:
            save_message(None, username, info["username"], text, is_dm=True, connection_type=connection_type)
            payload = json.dumps({
                "type": "dm",
                "sender": username,
                "recipient": info["username"],
                "text": text
            })
            await websocket.send(payload)
            if recipient_ws != websocket:
                await recipient_ws.send(payload)
        else:
            await websocket.send(json.dumps({
                "type": "system",
                "text": f"System Error: User '{recipient}' is not online in your network scope."
            }))

    elif msg_type == "typing":
        typing_state = bool(data.get("typing", False))
        if sender_info["typing"] != typing_state:
            sender_info["typing"] = typing_state
            await broadcast_user_list(connection_type)

    elif msg_type == "nick":
        new_nick = data.get("new_nick", "").strip()
        new_nick = "".join(c for c in new_nick if c.isalnum() or c in "_-")[:15]
        if not new_nick:
            await websocket.send(json.dumps({
                "type": "system",
                "text": "System Error: Invalid nickname."
            }))
            return

        # Enforce unique nicknames across active users of the same connection type
        existing = {info["username"].lower() for info in clients.values() if info["connection_type"] == connection_type}
        if new_nick.lower() in existing:
            await websocket.send(json.dumps({
                "type": "system",
                "text": f"System Error: Nickname '{new_nick}' is already taken."
            }))
            return

        old_nick = username
        actual_nick = get_unique_username(new_nick, connection_type)
        sender_info["username"] = actual_nick
        
        channel = sender_info["channel"]
        await broadcast_to_channel(channel, json.dumps({
            "type": "system",
            "text": f"System: ({old_nick}) is now known as ({actual_nick})."
        }), connection_type)
        
        await websocket.send(json.dumps({
            "type": "nick_ack",
            "new_nick": actual_nick
        }))
        
        await broadcast_user_list(connection_type)

    elif msg_type == "create":
        channel_name = data.get("channel", "").strip()
        if not channel_name.startswith("#") or len(channel_name) < 2 or len(channel_name) > 20:
            await websocket.send(json.dumps({
                "type": "system",
                "text": "System Error: Channel name must start with '#' and be 2-20 characters long."
            }))
            return
        
        clean_name = "#" + "".join(c for c in channel_name[1:] if c.isalnum() or c in "_-")
        if len(clean_name) < 2:
            await websocket.send(json.dumps({
                "type": "system",
                "text": "System Error: Invalid channel name."
            }))
            return

        success = add_channel(clean_name, username, connection_type)
        if success:
            await websocket.send(json.dumps({
                "type": "system",
                "text": f"System: Channel '{clean_name}' created successfully!"
            }))
            await broadcast_channel_list(connection_type)
        else:
            await websocket.send(json.dumps({
                "type": "system",
                "text": f"System Error: Channel '{clean_name}' already exists in your network scope."
            }))

    elif msg_type == "delete":
        channel_name = data.get("channel", "").strip()
        if channel_name == "#lobby":
            await websocket.send(json.dumps({
                "type": "system",
                "text": "System Error: You cannot delete the default #lobby channel."
            }))
            return

        creator = get_channel_creator(channel_name, connection_type)
        if not creator:
            await websocket.send(json.dumps({
                "type": "system",
                "text": f"System Error: Channel '{channel_name}' does not exist."
            }))
            return

        if creator != username and creator != "System":
            await websocket.send(json.dumps({
                "type": "system",
                "text": f"System Error: Only the creator ({creator}) can delete '{channel_name}'."
            }))
            return

        success = delete_channel(channel_name, connection_type)
        if success:
            # Move users back to #lobby
            targets = [ws for ws, info in clients.items() if info["channel"] == channel_name and info["connection_type"] == connection_type]
            for ws in targets:
                clients[ws]["channel"] = "#lobby"
                clients[ws]["typing"] = False
                await ws.send(json.dumps({
                    "type": "system",
                    "text": f"System: Channel {channel_name} was deleted. You were moved to #lobby."
                }))
                await ws.send(json.dumps({
                    "type": "join_ack",
                    "channel": "#lobby"
                }))
                
                # Resend lobby history to target client
                history = get_channel_history("#lobby", connection_type)
                for msg in history:
                    await ws.send(json.dumps(msg))

            await broadcast_channel_list(connection_type)
            await broadcast_user_list(connection_type)
            
            await websocket.send(json.dumps({
                "type": "system",
                "text": f"System: Channel '{channel_name}' deleted successfully!"
            }))
        else:
            await websocket.send(json.dumps({
                "type": "system",
                "text": "System Error: Failed to delete channel."
            }))

    elif msg_type == "kick":
        target = data.get("target", "").strip()
        channel = sender_info["channel"]
        
        if channel == "#lobby":
            await websocket.send(json.dumps({
                "type": "system",
                "text": "System Error: You cannot kick users from the default #lobby channel."
            }))
            return
            
        creator = get_channel_creator(channel, connection_type)
        if creator != username:
            await websocket.send(json.dumps({
                "type": "system",
                "text": f"System Error: Only the creator ({creator}) can kick users from '{channel}'."
            }))
            return

        # Find target client connection in the same channel & connection type
        target_ws = None
        for ws, info in clients.items():
            if info["username"].lower() == target.lower() and info["channel"] == channel and info["connection_type"] == connection_type:
                target_ws = ws
                break
                
        if target_ws:
            target_info = clients[target_ws]
            target_name = target_info["username"]
            
            # Move target user back to #lobby
            target_info["channel"] = "#lobby"
            target_info["typing"] = False
            
            # Notify the kicked user
            await target_ws.send(json.dumps({
                "type": "system",
                "text": f"System: You were kicked from {channel} by the owner."
            }))
            await target_ws.send(json.dumps({
                "type": "join_ack",
                "channel": "#lobby"
            }))
            
            # Send history for #lobby to kicked user
            history = get_channel_history("#lobby", connection_type)
            for msg in history:
                await target_ws.send(json.dumps(msg))
                
            # Notify channel about kick
            await broadcast_to_channel(channel, json.dumps({
                "type": "system",
                "text": f"System: ({target_name}) was kicked from the channel by the owner."
            }), connection_type)
            
            # Notify lobby about join
            await broadcast_to_channel("#lobby", json.dumps({
                "type": "system",
                "text": f"({target_name}) entered #lobby (kicked from {channel})."
            }), connection_type)
            
            await broadcast_user_list(connection_type)
            
            await websocket.send(json.dumps({
                "type": "system",
                "text": f"System: User '{target_name}' kicked successfully."
            }))
        else:
            await websocket.send(json.dumps({
                "type": "system",
                "text": f"System Error: User '{target}' is not in this channel."
            }))

async def handler(websocket):
    """Manage lifecycle and routing of a single websocket client session with public/local segregation."""
    # Retrieve Host header compatibly across different websockets library versions (v13+ vs v12-)
    host_values = []
    if hasattr(websocket, "request") and hasattr(websocket.request, "headers"):
        host_values = websocket.request.headers.get_all("Host")
    elif hasattr(websocket, "request_headers") and hasattr(websocket.request_headers, "get_all"):
        host_values = websocket.request_headers.get_all("Host")
    elif hasattr(websocket, "request_headers"):
        try:
            host_values = [websocket.request_headers.get("Host", "")]
        except Exception:
            host_values = []
        
    is_public = any(any(dom in h.lower() for dom in ["lhr.life", "localhost.run", "serveo.net", "serveousercontent.com", "pinggy", "ngrok"]) for h in host_values)
    connection_type = "public" if is_public else "local"

    logging.info(f"New client connected: Scope={connection_type.upper()}, Hosts={host_values}")
    
    try:
        init_message = await websocket.recv()
        data = json.loads(init_message)
    except Exception as e:
        logging.warning(f"Handshake failed: {e}")
        await websocket.close()
        return

    requested_user = data.get("user", "Anonymous").strip()
    requested_user = "".join(c for c in requested_user if c.isalnum() or c in "_-")[:15]
    if not requested_user:
        requested_user = "Anonymous"

    username = get_unique_username(requested_user, connection_type)
    clients[websocket] = {
        "username": username,
        "channel": "#lobby",
        "typing": False,
        "connection_type": connection_type
    }
    
    logging.info(f"User '{username}' registered in scope '{connection_type}'")
    
    try:
        await websocket.send(json.dumps({
            "type": "init_ack",
            "username": username,
            "channel": "#lobby",
            "channels": get_channels(connection_type)
        }))
    except Exception as e:
        logging.error(f"Error sending init_ack to '{username}': {e}", exc_info=True)
        return

    await broadcast_to_channel("#lobby", json.dumps({
        "type": "system",
        "text": f"({username}) joined the chat."
    }), connection_type)

    await broadcast_user_list(connection_type)

    if data.get("load_history", True):
        history = get_channel_history("#lobby", connection_type)
        for msg in history:
            await websocket.send(json.dumps(msg))

    try:
        async for message in websocket:
            await handle_client_message(websocket, message)
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        logging.error(f"Exception in client handler loop for '{username}': {e}", exc_info=True)
    finally:
        info = clients.pop(websocket, None)
        if info:
            u_name = info["username"]
            chan = info["channel"]
            logging.info(f"Client disconnected: User='{u_name}', Scope='{connection_type}'")
            await broadcast_to_channel(chan, json.dumps({
                "type": "system",
                "text": f"({u_name}) left the chat."
            }), connection_type)
            await broadcast_user_list(connection_type)

def get_local_ip():
    """Retrieve the primary local IP address of this machine."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def ensure_ssh_key():
    """Ensure a local SSH key exists inside the db/ directory for public tunnels to use."""
    key_path = os.path.join(ROOT_DIR, "db", "tunnel_key")
    if not os.path.exists(key_path):
        logging.info("Generating a new local SSH key for public tunnels...")
        try:
            import subprocess
            subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True
            )
            os.chmod(key_path, 0o600)
            logging.info("Successfully generated local SSH key.")
        except Exception as e:
            logging.warning(f"Failed to generate local SSH key: {e}")
            return None
    return key_path

# ==========================================================================
# Public tunnel providers
#
# There are 4 providers: ngrok, localhost.run, pinggy.io, serveo.net.
# Each provider coroutine returns (domain, process) on success or (None, None)
# on failure. `process` must expose .terminate() and an awaitable .wait().
# ==========================================================================

# Menu order / valid provider keys
TUNNEL_PROVIDERS = ["ngrok", "localhost.run", "pinggy", "serveo"]


async def _start_ngrok_tunnel():
    """Expose the server publicly via ngrok (requires NGROK_AUTHTOKEN)."""
    authtoken = (
        ENV.get("NGROK_AUTHTOKEN")
        or os.getenv("NGROK_AUTHTOKEN")
        or os.getenv("NGROK_AUTH_TOKEN")
    )
    if not authtoken:
        msg = "NGROK_AUTHTOKEN is not set — cannot start the ngrok tunnel."
        logging.warning(msg)
        print(f"--> {msg}")
        return None, None

    logging.info("Starting public internet tunnel via ngrok...")
    print("\n--> Launching secure public internet tunnel (via ngrok)...")
    try:
        # Kill any lingering ngrok processes on startup to prevent ERR_NGROK_334
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/IM", "ngrok.exe"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.run(["pkill", "-f", "ngrok"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

        import ssl
        ssl._create_default_https_context = ssl._create_unverified_context
        from pyngrok import conf, ngrok

        # Pin the agent + config to a writable, predictable location inside the
        # project so packaged/production builds (where ~/.config/ngrok may be
        # missing or read-only) still work.
        ngrok_dir = os.path.join(ROOT_DIR, "db", "ngrok")
        os.makedirs(ngrok_dir, exist_ok=True)
        bin_name = "ngrok.exe" if os.name == "nt" else "ngrok"

        pyngrok_config = conf.get_default()
        pyngrok_config.auth_token = authtoken
        pyngrok_config.ngrok_path = os.path.join(ngrok_dir, bin_name)
        pyngrok_config.config_path = os.path.join(ngrok_dir, "ngrok.yml")
        region = ENV.get("NGROK_REGION") or os.getenv("NGROK_REGION")
        if region:
            pyngrok_config.region = region.strip()

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: ngrok.install_ngrok(pyngrok_config))
        # Write the token into the pinned config explicitly (ngrok v3 exits with
        # ERR_NGROK_4018 otherwise, surfacing as "process unable to start").
        await loop.run_in_executor(None, lambda: ngrok.set_auth_token(authtoken, pyngrok_config))

        tunnel = await loop.run_in_executor(
            None, lambda: ngrok.connect(8765, "http", pyngrok_config=pyngrok_config)
        )
        if not tunnel:
            raise Exception("ngrok.connect() returned no tunnel object.")

        domain = tunnel.public_url.replace("https://", "").replace("http://", "")
        logging.info(f"Public tunnel allocated domain (ngrok): {domain}")

        class NgrokProc:
            def terminate(self):
                try:
                    ngrok.disconnect(tunnel.public_url)
                    ngrok.kill()
                except Exception:
                    pass
            async def wait(self):
                pass

        return domain, NgrokProc()
    except Exception as e:
        detail = str(e)
        ngrok_logs = getattr(e, "ngrok_logs", None)
        ngrok_error = getattr(e, "ngrok_error", None)
        if ngrok_error:
            detail = f"{detail} | {ngrok_error}"
        logging.warning(f"ngrok tunnel initialization failed: {detail}", exc_info=True)
        print(f"--> Ngrok tunnel failed: {detail}")
        if ngrok_logs:
            print("--> ngrok agent output:")
            for log_line in ngrok_logs[-12:]:
                line_text = getattr(log_line, "line", str(log_line))
                logging.warning(f"[ngrok] {line_text}")
                print(f"      {line_text}")
    return None, None


async def _start_localhostrun_tunnel(ssh_key_opts):
    """Expose the server publicly via localhost.run (SSH)."""
    logging.info("Starting public internet tunnel via localhost.run...")
    print("\n--> Launching secure public internet tunnel (via localhost.run)...")
    try:
        process = await asyncio.create_subprocess_exec(
            "ssh", "-T", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", *ssh_key_opts, "-R", "80:127.0.0.1:8765", "nokey@ssh.localhost.run",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        lines_read = []
        while True:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=30.0)
            except asyncio.TimeoutError:
                logging.warning("localhost.run connection timed out waiting for output.")
                break
            if not line:
                break
            line_str = line.decode().strip()
            if line_str:
                lines_read.append(line_str)
                match = re.search(r"https?://([a-zA-Z0-9\-]+\.(?:lhr\.life|lhr\.run|localhost\.run))", line_str)
                if match:
                    domain = match.group(1)
                    if domain != "admin.localhost.run":
                        logging.info(f"Public tunnel allocated domain (localhost.run): {domain}")
                        return domain, process
        logging.warning("localhost.run tunnel failed. Output/Errors:\n" + "\n".join(lines_read))
        try:
            process.terminate()
            await process.wait()
        except Exception:
            pass
    except Exception as e:
        logging.warning(f"localhost.run tunnel initialization failed: {e}")
    return None, None


async def _start_pinggy_tunnel(ssh_key_opts):
    """Expose the server publicly via pinggy.io (SSH)."""
    logging.info("Starting public internet tunnel via pinggy.io...")
    print("\n--> Launching secure public internet tunnel (via pinggy.io)...")
    try:
        process = await asyncio.create_subprocess_exec(
            "ssh", "-T", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-p", "443", *ssh_key_opts, "-R", "80:127.0.0.1:8765", "public@a.pinggy.io",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        lines_read = []
        while True:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=15.0)
            except asyncio.TimeoutError:
                logging.warning("pinggy.io connection timed out waiting for output.")
                break
            if not line:
                break
            line_str = line.decode().strip()
            if line_str:
                lines_read.append(line_str)
                match = re.search(r"https?://([a-zA-Z0-9\-]+\.(?:run\.pinggy-free\.link|free\.pinggy\.net|pinggy\.link|pinggy\.net))", line_str)
                if match:
                    domain = match.group(1)
                    logging.info(f"Public tunnel allocated domain (pinggy.io): {domain}")
                    return domain, process
        logging.warning("pinggy.io tunnel failed. Output/Errors:\n" + "\n".join(lines_read))
        try:
            process.terminate()
            await process.wait()
        except Exception:
            pass
    except Exception as e:
        logging.warning(f"pinggy.io tunnel initialization failed: {e}")
    return None, None


async def _start_serveo_tunnel(ssh_key_opts):
    """Expose the server publicly via serveo.net (SSH)."""
    logging.info("Starting public internet tunnel via serveo.net...")
    print("\n--> Launching secure public internet tunnel (via serveo.net)...")
    try:
        process = await asyncio.create_subprocess_exec(
            "ssh", "-T", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", *ssh_key_opts, "-R", "80:127.0.0.1:8765", "serveo.net",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        lines_read = []
        while True:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=15.0)
            except asyncio.TimeoutError:
                logging.warning("serveo.net connection timed out waiting for output.")
                break
            if not line:
                break
            line_str = line.decode().strip()
            if line_str:
                lines_read.append(line_str)
                match = re.search(r"https?://([a-zA-Z0-9\-]+\.(?:serveo\.net|serveousercontent\.com))", line_str)
                if match:
                    domain = match.group(1)
                    logging.info(f"Public tunnel allocated domain (serveo.net): {domain}")
                    return domain, process
        logging.warning("serveo.net tunnel failed. Output/Errors:\n" + "\n".join(lines_read))
        try:
            process.terminate()
            await process.wait()
        except Exception:
            pass
    except Exception as e:
        logging.error(f"serveo.net tunnel failed: {e}", exc_info=True)
    return None, None


def _ssh_available():
    """True if the system 'ssh' client is present (needed by SSH providers)."""
    return shutil.which("ssh") is not None


# Map of SSH-based providers (all require the 'ssh' binary)
_SSH_PROVIDERS = {
    "localhost.run": _start_localhostrun_tunnel,
    "pinggy": _start_pinggy_tunnel,
    "serveo": _start_serveo_tunnel,
}


async def start_tunnel(provider="auto"):
    """Start a public tunnel using the chosen provider.

    provider: 'ngrok' | 'localhost.run' | 'pinggy' | 'serveo' | 'auto'
      - A specific name uses ONLY that provider (no fallback).
      - 'auto' tries ngrok first (if a token exists), then the SSH providers
        in order until one succeeds.
    Returns (domain, process) on success or (None, None).
    """
    provider = (provider or "auto").strip().lower()
    key_path = ensure_ssh_key()
    ssh_key_opts = ["-i", key_path] if key_path else []

    # --- Single, explicitly chosen provider -------------------------------
    if provider == "ngrok":
        return await _start_ngrok_tunnel()

    if provider in _SSH_PROVIDERS:
        if not _ssh_available():
            msg = (f"'ssh' client not found, so the {provider} tunnel cannot "
                   "start. Install OpenSSH, or choose ngrok.")
            logging.error(msg)
            print(f"--> {msg}")
            print("--> Starting in Local-only mode.")
            return None, None
        return await _SSH_PROVIDERS[provider](ssh_key_opts)

    # --- Auto: ngrok (if token) then SSH providers as fallback ------------
    authtoken = (
        ENV.get("NGROK_AUTHTOKEN")
        or os.getenv("NGROK_AUTHTOKEN")
        or os.getenv("NGROK_AUTH_TOKEN")
    )
    if authtoken:
        domain, proc = await _start_ngrok_tunnel()
        if domain:
            return domain, proc
        print("--> Falling back to SSH options...")
    else:
        logging.info("NGROK_AUTHTOKEN not set; skipping ngrok, using SSH-based tunnels.")
        print("--> NGROK_AUTHTOKEN not set — skipping ngrok. Trying SSH-based tunnels...")

    if not _ssh_available():
        msg = ("'ssh' client not found, so localhost.run / pinggy / serveo "
               "tunnels cannot start. Set NGROK_AUTHTOKEN to use ngrok, or "
               "install OpenSSH.")
        logging.error(msg)
        print(f"--> {msg}")
        print("--> Starting in Local-only mode.")
        return None, None

    for name in ("localhost.run", "pinggy", "serveo"):
        domain, proc = await _SSH_PROVIDERS[name](ssh_key_opts)
        if domain:
            return domain, proc
        print(f"--> {name} tunnel failed. Trying next option...")

    print("--> All public tunnel configurations failed. Starting in Local-only mode.")
    return None, None


def resolve_tunnel_choice():
    """Decide the tunnel provider from CLI args / env, or None to ask the user.

    Returns one of: 'none', 'auto', 'ngrok', 'localhost.run', 'pinggy',
    'serveo', or None (meaning: prompt interactively).
    """
    valid = {"ngrok", "localhost.run", "pinggy", "serveo", "auto", "none"}

    if "--no-tunnel" in sys.argv:
        return "none"

    for i, arg in enumerate(sys.argv):
        if arg.startswith("--tunnel="):
            val = arg.split("=", 1)[1].strip().lower()
            return val if val in valid else "auto"
        if arg == "--tunnel":
            if i + 1 < len(sys.argv):
                nxt = sys.argv[i + 1].strip().lower()
                if nxt in valid:
                    return nxt
            return "auto"

    env_choice = (os.getenv("TUNNEL_PROVIDER") or ENV.get("TUNNEL_PROVIDER") or "").strip().lower()
    if env_choice in valid:
        return env_choice

    return None


def prompt_tunnel_choice():
    """Interactive numbered menu for picking the public tunnel provider."""
    options = [
        ("none",          "Local only  (no public tunnel)"),
        ("auto",          "Auto        (ngrok, then SSH fallbacks)"),
        ("ngrok",         "ngrok       (needs NGROK_AUTHTOKEN)"),
        ("localhost.run", "localhost.run  (SSH)"),
        ("pinggy",        "pinggy.io      (SSH)"),
        ("serveo",        "serveo.net     (SSH)"),
    ]

    header = "SELECT PUBLIC TUNNEL PROVIDER"
    body = [f"[{i}] {desc}" for i, (_, desc) in enumerate(options, start=1)]
    width = max([len(header)] + [len(b) for b in body])

    print("\n╔" + "═" * (width + 4) + "╗")
    print(f"║  {header:<{width + 1}} ║")
    print("╠" + "═" * (width + 4) + "╣")
    for b in body:
        print(f"║  {b:<{width + 1}} ║")
    print("╚" + "═" * (width + 4) + "╝")

    while True:
        try:
            raw = input(f"Choose an option [1-{len(options)}] (default 1): ").strip()
        except (IOError, OSError, EOFError):
            return "none"
        if raw == "":
            return options[0][0]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        low = raw.lower()
        for key, _ in options:
            if low == key:
                return key
        print("--> Invalid choice. Please enter a number from the list.")

async def repair_handshake_headers(connection, request):
    """Bypass corporate firewalls and ISP proxy blocks by dynamically restoring stripped handshake headers."""
    conn_val = request.headers.get("Connection")
    if not conn_val or "upgrade" not in conn_val.lower():
        request.headers["Connection"] = "Upgrade"
        
    upg_val = request.headers.get("Upgrade")
    if not upg_val or "websocket" not in upg_val.lower():
        request.headers["Upgrade"] = "websocket"
        
    return None

async def main():
    # Preemptive cleanup: Detect and kill any lingering process on port 8765
    kill_process_on_port(8765)
    
    show_progress("Initializing SQLite Database Engine", 0.6)
    init_db()
    
    # Decide which tunnel provider to use: CLI flag / env var, else ask the
    # user with an interactive menu. Non-interactive environments fall back to
    # Local-only safely.
    tunnel_choice = resolve_tunnel_choice()
    if tunnel_choice is None:
        try:
            tunnel_choice = prompt_tunnel_choice()
        except (IOError, OSError, EOFError):
            tunnel_choice = "none"

    tunnel_domain = None
    tunnel_proc = None
    if tunnel_choice and tunnel_choice != "none":
        show_progress("Establishing Public Internet Tunnel Connection", 1.2)
        tunnel_domain, tunnel_proc = await start_tunnel(tunnel_choice)
        
    local_ip = get_local_ip()
    
    # Gracefully handle port bind errors
    try:
        show_progress("Binding WebSockets Listener to Port 8765", 0.6)
        async with websockets.serve(handler, "0.0.0.0", 8765, process_request=repair_handshake_headers):
            logging.info("Porta Pro Server bound successfully on port 8765.")
            
            # Print configuration details in a beautiful double-bordered grid table
            print_config_table(local_ip, tunnel_domain)
            
            try:
                await asyncio.Future()
            finally:
                if tunnel_proc:
                    logging.info("Closing SSH tunnel...")
                    print("\n--> Gracefully closing SSH tunnel...")
                    try:
                        tunnel_proc.terminate()
                        await tunnel_proc.wait()
                    except ProcessLookupError:
                        pass
                    except Exception:
                        pass
    except OSError as e:
        # Check specifically for port bind address already in use (Errno 48 on macOS/Linux)
        if e.errno == 48 or "address already in use" in str(e).lower():
            err_msg = "Error: Port 8765 is already in use. Please verify if another server instance is running."
            logging.critical(err_msg, exc_info=True)
            print(f"\n[CRITICAL ERROR] {err_msg}")
            sys.exit(1)
        else:
            # Handle other OS errors: print cleanly in production, raise traceback in development
            logging.critical(f"Server OS error: {e}", exc_info=True)
            if IS_PRODUCTION:
                print(f"\n[CRITICAL ERROR] Server error occurred: {e}")
                sys.exit(1)
            else:
                raise e

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Server process terminated via KeyboardInterrupt.")
        print("\nServer terminated.")
        sys.exit(0)