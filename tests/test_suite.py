import asyncio
import os
import websockets
import json
import sqlite3
import subprocess
import time
import sys

# Test configuration
SERVER_URI = "ws://localhost:8765"
tests_passed = []
tests_failed = []

def log_test(name, success, error_msg=""):
    if success:
        tests_passed.append(name)
        print(f"✅ [PASS] {name}")
    else:
        tests_failed.append(name)
        print(f"❌ [FAIL] {name} - {error_msg}")

async def clear_queue(ws):
    """Drain any pending messages in the websocket queue."""
    while True:
        try:
            await asyncio.wait_for(ws.recv(), timeout=0.05)
        except asyncio.TimeoutError:
            break

def connect_with_host(uri, host=None):
    """Helper to connect to WebSocket with custom Host header, compatible with different websockets library versions."""
    if not host:
        return websockets.connect(uri)
    try:
        return websockets.connect(uri, additional_headers={"Host": host})
    except TypeError:
        return websockets.connect(uri, extra_headers={"Host": host})

async def run_suite():
    print("==================================================")
    print(" Starting Porta Automated Test Suite (Segregation & Kick)")
    print("==================================================\n")

    # Start the server locally in a subprocess
    print("--> Launching local chat server...")
    server_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../server/server.py")
    server_proc = subprocess.Popen(
        [sys.executable, server_path, "--no-tunnel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    time.sleep(4.0) # Allow server to bind (accommodating auto-kill socket freeing and progress bars)

    # Check if server is running
    if server_proc.poll() is not None:
        print("❌ Server failed to start. Port might be occupied.")
        sys.exit(1)

    try:
        # TEST 1: Local / Public Client Connection
        print("\n[Test 1] Connecting local and public clients...")
        # A connects locally; B connects simulating a public tunnel via Host header
        async with connect_with_host(SERVER_URI) as ws_local_a, \
                   connect_with_host(SERVER_URI, host="abc.lhr.life") as ws_public_b:
            log_test("Local & Public Dual WebSocket Connections", True)

            # Handshakes
            await ws_local_a.send(json.dumps({"type": "init", "user": "joker", "load_history": False}))
            await ws_public_b.send(json.dumps({"type": "init", "user": "harley", "load_history": False}))

            ack_a = json.loads(await ws_local_a.recv())
            ack_b = json.loads(await ws_public_b.recv())

            log_test("Local Client Connected as 'joker'", ack_a.get("username") == "joker")
            log_test("Public Client Connected as 'harley'", ack_b.get("username") == "harley")

            # TEST 2: Environment Isolation (User Lists)
            print("\n[Test 2] Checking environment isolation...")
            # A receives its own join log and user list
            join_msg_a = json.loads(await ws_local_a.recv())
            user_list_a = json.loads(await ws_local_a.recv())
            
            # B receives its own join log and user list
            join_msg_b = json.loads(await ws_public_b.recv())
            user_list_b = json.loads(await ws_public_b.recv())

            # Verify system message bracket fix: must contain parentheses () instead of brackets []
            log_test("System Message Parentheses Bracket Fix (Join)", 
                     "(joker) joined the chat." in join_msg_a.get("text", "") and 
                     "(harley) joined the chat." in join_msg_b.get("text", ""))

            # Verify user lists only contain users of their respective scopes
            a_users = [u["username"] for u in user_list_a.get("users", [])]
            b_users = [u["username"] for u in user_list_b.get("users", [])]
            
            log_test("Local Client User List Isolation (Only local users)", "joker" in a_users and "harley" not in a_users)
            log_test("Public Client User List Isolation (Only public users)", "harley" in b_users and "joker" not in b_users)

            # TEST 3: Message Segregation
            # Local client sends a message. Public client should receive nothing.
            print("\n[Test 3] Verifying message segregation...")
            await ws_local_a.send(json.dumps({"type": "chat", "text": "This is a local secret"}))
            
            # Client A receives own message
            await ws_local_a.recv()
            
            # Client B tries to receive but should timeout (receiving nothing)
            received_by_b = True
            try:
                await asyncio.wait_for(ws_public_b.recv(), timeout=0.1)
            except asyncio.TimeoutError:
                received_by_b = False
            log_test("Public Clients Isolated from Local Chat Messages", not received_by_b)

            await clear_queue(ws_local_a)
            await clear_queue(ws_public_b)

            # TEST 4: Nickname Duplicates (Across Different Scopes)
            # A public client connects with name 'joker' (since it is a different scope, it should NOT get index '_2')
            print("\n[Test 4] Verifying nickname rules across isolated scopes...")
            async with connect_with_host(SERVER_URI, host="abc.lhr.life") as ws_public_c:
                await ws_public_c.send(json.dumps({"type": "init", "user": "joker", "load_history": False}))
                ack_c = json.loads(await ws_public_c.recv())
                
                log_test("Same Name Allowed Across Isolated Scopes", ack_c.get("username") == "joker")
                
                # Drain B and C queues
                await clear_queue(ws_public_b)
                await clear_queue(ws_public_c)

            # TEST 5: Owner Kick Command
            # Client B creates channel '#testing' in public scope
            print("\n[Test 5] Setting up Owner Kick test...")
            await ws_public_b.send(json.dumps({"type": "create", "channel": "#testing"}))
            await clear_queue(ws_public_b)
            
            # Connect Client C (harley_2) in public scope and join '#testing'
            async with connect_with_host(SERVER_URI, host="abc.lhr.life") as ws_public_c:
                await ws_public_c.send(json.dumps({"type": "init", "user": "harley", "load_history": False}))
                ack_c = json.loads(await ws_public_c.recv())
                username_c = ack_c.get("username") # Should be 'harley_2' due to index check in public scope
                log_test("Unique Nickname Index within Same Scope", username_c == "harley_2")
                
                await clear_queue(ws_public_b)
                await clear_queue(ws_public_c)
                
                # Client C joins '#testing'
                await ws_public_c.send(json.dumps({"type": "join", "channel": "#testing"}))
                await clear_queue(ws_public_b)
                await clear_queue(ws_public_c)
                
                # Client B (owner) kicks Client C (harley_2)
                print("[Test 5] Executing owner kick command...")
                await ws_public_b.send(json.dumps({"type": "kick", "target": "harley_2"}))
                
                # Client C receives kick warning, join_ack for lobby, and user list updates
                kick_system_msg = json.loads(await ws_public_c.recv())
                kick_join_ack = json.loads(await ws_public_c.recv())
                
                kicked_correctly = "kicked" in kick_system_msg.get("text", "") and kick_join_ack.get("channel") == "#lobby"
                log_test("Kicked Client Automatically Returned to Lobby", kicked_correctly)
                
                # Read Client B's log (B should receive System kick confirmation)
                kick_conf_b = json.loads(await ws_public_b.recv())
                log_test("Owner Receives Kick Confirmation", "kicked successfully" in kick_conf_b.get("text", ""))

    except Exception as e:
        print(f"\n❌ Test suite execution encountered an exception: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Terminate server subprocess
        print("\n--> Stopping local server...")
        server_proc.terminate()
        stdout_data, stderr_data = server_proc.communicate()
        print("\n--- SERVER STDOUT ---")
        print(stdout_data.decode())
        print("--- SERVER STDERR ---")
        print(stderr_data.decode())
        print("---------------------")

    # PRINT SUMMARY
    print("\n" + "="*50)
    print(" TEST SUITE SUMMARY")
    print("="*50)
    print(f" Passed: {len(tests_passed)} / {len(tests_passed) + len(tests_failed)}")
    print(f" Failed: {len(tests_failed)}")
    print("="*50)
    
    if tests_failed:
        sys.exit(1)
    else:
        sys.exit(0)

if __name__ == "__main__":
    asyncio.run(run_suite())
