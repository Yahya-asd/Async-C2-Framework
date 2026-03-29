# Async-C2-Framework
An asynchronous Python Command and Control (C2) framework developed as a Proof of Concept (PoC) for studying network communication, encrypted payloads, and endpoint detection strategies.



# Async-C2-Framework 🛡️

A custom, asynchronous Command & Control (C2) framework written in Python. This project utilizes the `asyncio` library for non-blocking concurrent client handling and Fernet symmetric encryption for secure payload delivery. 

It was developed to understand how modern post-exploitation frameworks maintain persistent communication and to study network traffic signatures for better defensive engineering.

## ⚠️ Educational Disclaimer
**This project is strictly for academic research and authorized penetration testing environments.** The author does not endorse, encourage, or support any illegal or malicious use of this code. All active exploitation modules (such as UAC bypasses and registry persistence) have been intentionally removed or neutered. You are solely responsible for your actions. Do not deploy this on systems you do not own or do not have explicit permission to test.

## ⚙️ Architecture & Features
* **Asynchronous I/O:** Both the server and agent utilize Python's `asyncio` to handle multiple concurrent tasks (like keylogging and shell execution) without blocking the main connection thread.
* **Encrypted Channels:** All JSON payloads are serialized and wrapped in Fernet symmetric encryption before transit.
* **Dynamic Tasking:** The dispatcher queues commands dynamically, allowing the server to issue shell commands, request system information, or pull screenshots over the established socket.

## 🔍 Detection & Mitigation (Blue Team Notes)

Understanding how to detect this traffic is the primary goal of this repository. If deployed in a lab environment, analysts can use standard security tools to spot anomalies:

* **Wireshark / Packet Analysis:** While the payload is encrypted, the protocol uses a fixed 4-byte big-endian length header preceding the ciphertext. Analysts filtering TCP streams in Wireshark will notice a recurring `[Length][Encrypted Blob]` pattern that does not conform to standard TLS/SSL handshakes.
* **Nmap Profiling:** The C2 server listens on a custom port (default `4444`). A standard `nmap -sV` scan against the server infrastructure will likely fail to identify the service, flagging it as `unknown`, which is highly suspicious for a listening port on an external boundary.
* **Endpoint Behavior:** The agent frequently spawns `cmd.exe` or `powershell.exe` as child processes (via `subprocess.getoutput`) to execute commands. EDR solutions can flag this behavior by monitoring for unusual process trees originating from a Python runtime.

## 💻 Setup & Usage (Lab Environment Only)

1. Generate a new Fernet key and update the `KEY` variable in both scripts.
2. Run the server:
   ```bash
   python c2_server_final.py
