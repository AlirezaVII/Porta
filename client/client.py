import os
import getpass
import asyncio
import json
import sys
import ssl
import logging
import time

# Project root directory
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if "__file__" in globals() else os.getcwd()

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

ENV = load_env()
CONFIG_STATE = ENV.get("CONFIGUATION_STATE", "0").strip()
IS_PRODUCTION = (CONFIG_STATE == "1")

# Setup logging
LOG_FILE = os.path.join(ROOT_DIR, "porta.log")
logging.basicConfig(
    filename=LOG_FILE,
    filemode="a",
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

try:
    import pyfiglet
    import websockets
    from textual.app import App, ComposeResult
    from textual.widgets import Header, Footer, Input, RichLog, Static
    from textual.containers import Container, Vertical
except ImportError as e:
    logging.critical(f"Dependency import error: {e}", exc_info=True)
    print("Please install dependencies using 'pip install -r requirements.txt'")
    sys.exit(1)

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

# Show banner
print("\033[95m" + pyfiglet.figlet_format("Porta", font="slant") + "\033[0m")
print("\033[96m" + "Welcome to Porta Pro!" + "\033[0m")
print("Loading core elements...\n")

# 2. Ask for history
load_history_input = input("Do you want to load chat history? (Y/n): ").strip().lower()
load_history = load_history_input != 'n'

# 3. Ask for server IP
server_ip = input("Enter server IP (default: localhost): ").strip()
if not server_ip:
    server_ip = "localhost"

print()
show_progress("Resolving Server Host Connection Target", 0.6)
show_progress("Establishing Active Handshake Exchange", 0.6)
show_progress("Loading Chat and Direct Message Profiles", 0.6)
show_progress("Initializing Textual TUI Screen Elements", 0.6)
print()

# 4. Get default username
USERNAME = getpass.getuser()

class ChatApp(App):
    CSS = """
    #main-layout {
        layout: horizontal;
        height: 1fr;
    }
    
    #sidebar {
        width: 28;
        height: 100%;
        background: #11111b;
        border-right: tall #89b4fa;
        padding: 0 1;
    }
    
    #channels-header, #users-header {
        margin-top: 1;
        content-align: center middle;
        text-style: bold;
    }
    
    #channels-box {
        height: 35%;
        border: round #89b4fa;
        background: #181825;
    }
    
    #users-box {
        height: 55%;
        border: round #89b4fa;
        background: #181825;
    }
    
    #chat-area {
        height: 100%;
        width: 1fr;
        layout: vertical;
    }
    
    #chat-log {
        height: 1fr;
        border: round #89b4fa;
        background: #181825;
        margin: 1 2 0 2;
    }
    
    #typing-indicator {
        height: 1;
        margin: 0 3;
        color: #bac2de;
        text-style: italic;
    }
    
    #message-input {
        margin: 0 2 1 2;
        border: round #a6e3a1;
        background: #313244;
    }
    
    /* Theme Dracula styles */
    .theme-dracula #sidebar {
        background: #191a21;
        border-right: tall #bd93f9;
    }
    .theme-dracula #channels-box {
        border: round #bd93f9;
        background: #21222c;
    }
    .theme-dracula #users-box {
        border: round #bd93f9;
        background: #21222c;
    }
    .theme-dracula #chat-log {
        border: round #bd93f9;
        background: #21222c;
    }
    .theme-dracula #message-input {
        border: round #ff79c6;
        background: #44475a;
    }
    
    /* Theme Latte styles */
    .theme-latte #sidebar {
        background: #dce0e8;
        border-right: tall #1e66f5;
    }
    .theme-latte #channels-box {
        border: round #1e66f5;
        background: #e6e9ef;
    }
    .theme-latte #users-box {
        border: round #1e66f5;
        background: #e6e9ef;
    }
    .theme-latte #chat-log {
        border: round #1e66f5;
        background: #e6e9ef;
    }
    .theme-latte #message-input {
        border: round #40a02b;
        background: #ccd0da;
    }
    """
    
    def __init__(self, load_history_flag: bool, server_ip: str):
        super().__init__()
        self.load_history_flag = load_history_flag
        self.server_ip = server_ip
        self.websocket = None
        self.connection_task = None
        
        self.current_channel = "#lobby"
        self.channels = ["#lobby"]
        self.is_typing = False
        self.typing_timer = None
        self.chat_theme = "mocha"

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="main-layout"):
            with Container(id="sidebar"):
                yield Static("[bold #cba6f7]CHANNELS[/bold #cba6f7]", id="channels-header")
                yield RichLog(id="channels-box", auto_scroll=False, markup=True)
                yield Static("[bold #f38ba8]ONLINE USERS[/bold #f38ba8]", id="users-header")
                yield RichLog(id="users-box", auto_scroll=False, markup=True)
            with Container(id="chat-area"):
                yield RichLog(id="chat-log", highlight=True, markup=True)
                yield Static("", id="typing-indicator")
                yield Input(placeholder="Type a message or /help for commands...", id="message-input")
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "Porta Pro Chat"
        self.chat_log = self.query_one("#chat-log", RichLog)
        self.channels_box = self.query_one("#channels-box", RichLog)
        self.users_box = self.query_one("#users-box", RichLog)
        self.typing_indicator = self.query_one("#typing-indicator", Static)
        
        self.update_theme()
        
        # Start connection manager loop
        self.connection_task = asyncio.create_task(self.run_connection())

    def update_theme(self):
        self.screen.remove_class("theme-mocha", "theme-dracula", "theme-latte")
        self.screen.add_class(f"theme-{self.chat_theme}")

    async def run_connection(self):
        while True:
            self.chat_log.write("[bold yellow]Connecting to Porta server...[/bold yellow]")
            try:
                # Auto-detect ws:// vs wss:// based on destination host.
                # Standard local configurations use ws://, while internet tunnels require wss://.
                is_local = (
                    self.server_ip == "localhost" or 
                    self.server_ip == "127.0.0.1" or 
                    self.server_ip.startswith("192.168.") or 
                    self.server_ip.startswith("172.") or 
                    self.server_ip.startswith("10.")
                )
                
                protocol = "ws" if is_local else "wss"
                
                # Exclude port suffix if already configured (e.g. for subdomains/domains)
                if ":" in self.server_ip or self.server_ip.endswith(".run") or self.server_ip.endswith(".life"):
                    # Tunnels map directly to port 80/443, so no port suffix is required
                    uri = f"{protocol}://{self.server_ip}"
                else:
                    uri = f"{protocol}://{self.server_ip}:8765"
                
                ssl_context = None
                if protocol == "wss":
                    ssl_context = ssl.create_default_context()
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_NONE
                
                async with websockets.connect(uri, ssl=ssl_context) as ws:
                    self.websocket = ws
                    self.chat_log.write("[bold green]Connected successfully![/bold green]\n")
                    self.chat_log.write("Type [bold]/help[/bold] to see list of slash commands.\n")
                    
                    # Handshake
                    await ws.send(json.dumps({
                        "type": "init",
                        "user": USERNAME,
                        "load_history": self.load_history_flag
                    }))
                    
                    # Listener loop
                    async for message in ws:
                        await self.handle_message(message)
            except Exception as e:
                self.websocket = None
                logging.error(f"Client connection error/loss: {e}", exc_info=True)
                if IS_PRODUCTION:
                    self.chat_log.write("[bold red]Connection failed or lost. Reconnecting...[/bold red]")
                else:
                    self.chat_log.write(f"[bold red]Connection failed or lost: {e}[/bold red]")
                self.chat_log.write("[italic gray]Retrying in 5 seconds...[/italic gray]")
                await asyncio.sleep(5)

    async def handle_message(self, message_str: str):
        try:
            data = json.loads(message_str)
        except json.JSONDecodeError:
            return

        msg_type = data.get("type")
        
        if msg_type == "init_ack":
            global USERNAME
            USERNAME = data["username"]
            self.current_channel = data["channel"]
            self.channels = data["channels"]
            self.sub_title = f"Logged in as: {USERNAME} | Channel: {self.current_channel}"
            self.update_channels_sidebar()

        elif msg_type == "join_ack":
            self.current_channel = data["channel"]
            self.sub_title = f"Logged in as: {USERNAME} | Channel: {self.current_channel}"
            self.chat_log.clear()
            self.chat_log.write(f"\n[bold green]Entered channel {self.current_channel} as {USERNAME}[/bold green]\n")
            self.update_channels_sidebar()

        elif msg_type == "channel_list":
            self.channels = data["channels"]
            self.update_channels_sidebar()

        elif msg_type == "nick_ack":
            USERNAME = data["new_nick"]
            self.sub_title = f"Logged in as: {USERNAME} | Channel: {self.current_channel}"

        elif msg_type == "user_list":
            self.update_users_sidebar(data["users"])

        elif msg_type == "chat":
            channel = data.get("channel")
            user = data.get("user")
            text = data.get("text")
            if channel == self.current_channel:
                if user == USERNAME:
                    self.chat_log.write(f"[bold #a6e3a1]{user}[/bold #a6e3a1]: {text}")
                else:
                    self.chat_log.write(f"[bold #89b4fa]{user}[/bold #89b4fa]: {text}")

        elif msg_type == "dm":
            sender = data.get("sender")
            recipient = data.get("recipient")
            text = data.get("text")
            # Show Direct Message with distinctive red/pink styling
            if sender == USERNAME:
                self.chat_log.write(f"[bold italic #f38ba8]DM to {recipient}:[/bold italic #f38ba8] {text}")
            else:
                self.chat_log.write(f"[bold italic #f38ba8]DM from {sender}:[/bold italic #f38ba8] {text}")

        elif msg_type == "system":
            text = data.get("text")
            self.chat_log.write(f"[italic #bac2de]{text}[/italic #bac2de]")

    def update_channels_sidebar(self):
        self.channels_box.clear()
        for ch in self.channels:
            if ch == self.current_channel:
                self.channels_box.write(f"[bold #cba6f7]> {ch}[/bold #cba6f7]")
            else:
                self.channels_box.write(f"  {ch}")

    def update_users_sidebar(self, users):
        self.users_box.clear()
        typing_users = []
        for u in users:
            name = u["username"]
            channel = u["channel"]
            is_typing = u["typing"]
            
            is_same_chan = channel == self.current_channel
            status_marker = " ✍" if (is_typing and is_same_chan) else ""
            if is_typing and is_same_chan and name != USERNAME:
                typing_users.append(name)
                
            self.users_box.write(f"[bold #a6e3a1]•[/bold #a6e3a1] {name}{status_marker} [gray]({channel})[/gray]")
        
        # Update inline typing indicator
        if typing_users:
            names = ", ".join(typing_users)
            verb = "is" if len(typing_users) == 1 else "are"
            self.typing_indicator.update(f"{names} {verb} typing...")
        else:
            self.typing_indicator.update("")

    async def on_input_changed(self, event: Input.Changed) -> None:
        if self.websocket:
            # Send typing status
            if not self.is_typing:
                self.is_typing = True
                await self.websocket.send(json.dumps({
                    "type": "typing",
                    "typing": True
                }))
            
            # Reset the debounce timer
            if self.typing_timer:
                self.typing_timer.cancel()
            self.typing_timer = asyncio.create_task(self.stop_typing_after_delay())

    async def stop_typing_after_delay(self):
        await asyncio.sleep(2.5)
        if self.is_typing and self.websocket:
            self.is_typing = False
            await self.websocket.send(json.dumps({
                "type": "typing",
                "typing": False
            }))

    async def on_input_submitted(self, message: Input.Submitted) -> None:
        val = message.value.strip()
        if not val:
            return
        
        # Clear input box
        self.query_one("#message-input", Input).value = ""
        
        # Handle slash commands
        if val.startswith("/"):
            parts = val.split(" ", 2)
            cmd = parts[0].lower()
            
            if cmd == "/help":
                self.chat_log.write("\n[bold #cba6f7]Available Commands:[/bold #cba6f7]")
                self.chat_log.write("  [bold]/join #channel[/bold] - Switch channels")
                self.chat_log.write("  [bold]/create #channel[/bold] - Create a new channel (you will be the owner)")
                self.chat_log.write("  [bold]/delete #channel[/bold] - Delete a channel (creator only)")
                self.chat_log.write("  [bold]/kick username[/bold] - Kick a user from your channel (creator only)")
                self.chat_log.write("  [bold]/msg username message[/bold] - Direct message another online user")
                self.chat_log.write("  [bold]/nick new_name[/bold] - Change your display nickname")
                self.chat_log.write("  [bold]/theme [mocha|dracula|latte][/bold] - Switch UI color theme")
                self.chat_log.write("  [bold]/clear[/bold] - Clear current chat screen")
                self.chat_log.write("  [bold]/help[/bold] - Display this commands help menu")
                self.chat_log.write("  [bold]/exit[/bold] - Quit the chat app\n")
                
            elif cmd == "/join":
                if len(parts) < 2:
                    self.chat_log.write("[bold red]Usage: /join #channel-name[/bold red]")
                    return
                channel = parts[1]
                if self.websocket:
                    await self.websocket.send(json.dumps({
                        "type": "join",
                        "channel": channel
                    }))
                    
            elif cmd == "/create":
                if len(parts) < 2:
                    self.chat_log.write("[bold red]Usage: /create #channel-name[/bold red]")
                    return
                channel = parts[1]
                if self.websocket:
                    await self.websocket.send(json.dumps({
                        "type": "create",
                        "channel": channel
                    }))

            elif cmd == "/delete":
                if len(parts) < 2:
                    self.chat_log.write("[bold red]Usage: /delete #channel-name[/bold red]")
                    return
                channel = parts[1]
                if self.websocket:
                    await self.websocket.send(json.dumps({
                        "type": "delete",
                        "channel": channel
                    }))

            elif cmd == "/kick":
                if len(parts) < 2:
                    self.chat_log.write("[bold red]Usage: /kick username[/bold red]")
                    return
                target = parts[1]
                if self.websocket:
                    await self.websocket.send(json.dumps({
                        "type": "kick",
                        "target": target
                    }))
                    
            elif cmd == "/msg":
                if len(parts) < 3:
                    self.chat_log.write("[bold red]Usage: /msg username message[/bold red]")
                    return
                recipient = parts[1]
                text = parts[2]
                if self.websocket:
                    await self.websocket.send(json.dumps({
                        "type": "dm",
                        "recipient": recipient,
                        "text": text
                    }))
                    
            elif cmd == "/nick":
                if len(parts) < 2:
                    self.chat_log.write("[bold red]Usage: /nick new_name[/bold red]")
                    return
                new_nick = parts[1]
                if self.websocket:
                    await self.websocket.send(json.dumps({
                        "type": "nick",
                        "new_nick": new_nick
                    }))
                    
            elif cmd == "/theme":
                if len(parts) < 2:
                    self.chat_log.write("[bold red]Usage: /theme [mocha|dracula|latte][/bold red]")
                    return
                theme = parts[1].lower()
                if theme in ["mocha", "dracula", "latte"]:
                    self.chat_theme = theme
                    self.update_theme()
                    self.chat_log.write(f"[bold green]Theme switched to {theme}![/bold green]")
                else:
                    self.chat_log.write("[bold red]Invalid theme. Choose: mocha, dracula, latte[/bold red]")
                    
            elif cmd == "/clear":
                self.chat_log.clear()
                
            elif cmd == "/exit":
                self.exit()
                
            else:
                self.chat_log.write(f"[bold red]Unknown command: {cmd}. Type /help for options.[/bold red]")
        else:
            # Regular chat message
            if self.websocket:
                # Cancel typing timer and stop typing indicator
                if self.typing_timer:
                    self.typing_timer.cancel()
                self.is_typing = False
                await self.websocket.send(json.dumps({
                    "type": "typing",
                    "typing": False
                }))
                
                await self.websocket.send(json.dumps({
                    "type": "chat",
                    "text": val
                }))

    async def on_unmount(self) -> None:
        if self.connection_task:
            self.connection_task.cancel()
        if self.typing_timer:
            self.typing_timer.cancel()
        if self.websocket:
            try:
                await self.websocket.close()
            except Exception:
                pass

if __name__ == "__main__":
    try:
        app = ChatApp(load_history, server_ip)
        app.run()
    except KeyboardInterrupt:
        print("\nExit chat app.")
        sys.exit(0)
    except Exception as e:
        logging.critical(f"Unexpected client exception: {e}", exc_info=True)
        if IS_PRODUCTION:
            print("\n[Error] An unexpected application error occurred. Details have been logged to 'porta.log'.")
            sys.exit(1)
        else:
            raise e
