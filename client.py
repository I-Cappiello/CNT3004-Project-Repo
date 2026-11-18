import os
import socket
import ssl
import time
import uuid


from protocol import CHUNK, VERSION, ProtocolError, receive_message, send_message, sha256_file

def new_id():
    return uuid.uuid4().hex

def main():
    conn = None
    connected = False
    print("To connect to the server, enter 'connect <server IP Address> <server Port #>'.")

    while True:
        user_input = input(">").strip()
        if not user_input:
            continue

        data = user_input.split(" ", 2)
        cmd = data[0].lower()

        # connection and authentication
        if cmd == "connect":
            if len(data) < 3:
                print("Error: please enter IP and port")
                continue

            ip = data[1]
            port = int(data[2])
            ca_file = os.path.join("certs", "ca.crt")

            if not os.path.exists(ca_file):
                print("[ERROR] Missing certs/ca.crt file")
                continue

            try:
                ctx = ssl.create_default_context(cafile=ca_file)
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                raw_sock.settimeout(15)
                raw_sock.connect((ip, port))
                conn = ctx.wrap_socket(raw_sock, server_hostname=ip)
                conn.settimeout(120)
                print("[OK] Connected to server!")

                user = input("Username: ")
                pw = input("Password: ")

                send_message(conn, {
                    "op": "auth",
                    "version": VERSION,
                    "username": user,
                    "password": pw
                })
                auth_res = receive_message(conn)

                if auth_res.get("status") == "ok":
                    print("[OK] Authentication successful!")
                    connected = True
                    print("\n========== COMMAND MENU ==========")
                    print("1. upload <filename>")
                    print("2. download <filename>")
                    print("3. delete <filename>")
                    print("4. dir")
                    print("5. subfolder create <directory>")
                    print("6. subfolder delete <directory>")
                    print("7. disconnect")
                    print("=====================================")
                else:
                    print("[ERROR] Connection failed")
                    if conn:
                        conn.close()
                        conn = None
            except(OSError, ssl.SSLError, ProtocolError, EOFError):
                print("[ERROR] Connection failed")
                if conn:
                    conn.close()
                    conn = None

        elif not connected:
            print("Please connect to the server first.")

        # Upload the file
        elif cmd == "upload":
            if len(data) < 2:
                print("Error: missing filename")
                continue

            filepath = data[1]
            if not os.path.isfile(filepath):
                print("[ERROR] File not found")
                continue

            filename = os.path.basename(filepath)
            file_size = os.path.getsize(filepath)
            file_hash = sha256_file(filepath)

            t1 = time.time()
            send_message(conn, {
                "id": uuid.uuid4().hex,
                "op": "upload",
                "path": filename,
                "size": file_size,
                "sha256": file_hash,
            })
            res = receive_message(conn)
            resp_time = (time.time() - t1) * 1000

            # handle duplicate file overwrite prompt
            if res.get("status") == "exists":
                ans = input("File exists. Overwrite? (y/n): ")
                do_overwrite = (ans.strip().lower() == "y")
                send_message(conn, {"overwrite": do_overwrite})
                res = receive_message(conn)
                if not do_overwrite:
                    print("Upload cancelled")
                    continue

                if res.get("status") != "ready":
                    print(f"[ERROR] {res.get('message', 'Upload failed')}")
                    continue

                print(f"Uploading {filename}...")
                start = time.time()
                sent = 0
                old_p = -1

                f = open(filepath, "rb")
                while True:
                    bytes_read = f.read(CHUNK)
                    if not bytes_read:
                        break
                    conn.sendall(bytes_read)
                    sent += len(bytes_read)
                    if file_size > 0:
                        p = int((sent / file_size) * 100)
                        if p != old_p:
                            print(f"Progress: {p}%", end="\r")
                            old_p = p

                        f.close()

                        done_res = receive_message(conn)
                        total_time = time.time() - start
                        if total_time == 0:
                            total_time = 0.001

                        mb_size = file_size / (1024 * 1024)
                        rate = mb_size / total_time

                        if done_res.get("status") == "ok":
                            print(f"\nSize: {mb_size:.2f} MB")
                            print(f"Transfer Time: {total_time:.2f} seconds")
                            print(f"Average Size: {rate:.2f} MB/s")
                            print(f"Response Time: {resp_time:.2f} ms")
                            print(f"[OK] Upload completed.")
                        else:
                            print(f"\n[ERROR] {done_res.get('message' , 'Upload error')}")

        # download file (with interrupted transfer resume)
        elif cmd == "download":
            if len(data) < 2:
                print("Error: missing filename")
                continue

            remote_name = data[1]
            local_name = os.path.basename(remote_name)
            offset = 0
            saved_hash = ""

            # checks if a partial file exists from an interrupted download
            if os.path.isfile(local_name):
                part_size = os.path.getsize(local_name)
                if part_size > 0:
                    ans = input(f"Partial file found ({part_size} bytes). Resume? (y/n): ")
                    if ans.strip().lower() == "y":
                        offset = part_size
                        saved_hash = input("Enter file SHA-256 to resume (or press Enter to restart):").strip().lower()
                        if len(saved_hash) != 64:
                            offset = 0

            req = {
                "id": uuid.uuid4().hex,
                "op": "download",
                "path": remote_name,
                "offset": offset
            }
            if offset > 0:
                req["sha256"] = saved_hash

            t1 = time.time()
            send_message(conn, req)
            res = receive_message(conn)
            resp_time = (time.time() - t1) * 1000

            if res.get("status") != "ready":
                print(f"[ERROR] {res.get('message', 'File not found')}")
                continue

            file_size = res["size"]
            start_offset = res.get("offset", 0)
            expected_hash = res["sha256"]
            send_message(conn, {"accept": True})

            print(f"Downloading {remote_name} (SHA-256: {expected_hash})...")
            start = time.time()
            received = start_offset
            old_p = -1
            file_mode = "ab" if start_offset > 0 else "wb"

            try:
                f = open(local_name, file_mode)
                while received < file_size:
                    left = file_size - received
                    if left < CHUNK:
                        chunk = conn.recv(left)
                    else:
                        chunk = conn.recv(CHUNK)
                    if not chunk:
                        raise EOFError("Connection lost")
                    f.write(chunk)
                    received += len(chunk)
                    if file_size > 0:
                        p = int((received / file_size) * 100)
                        if p != old_p:
                            print(f"Progress: {p}%", end="\r")
                            old_p = p
                f.close()
            except (OSError, EOFError):
                f.close()
                print(f"\n[ERROR] Download interrupted at {received}/{file_size} bytes. Partial file saved.")
                connected = False
                break

            actual_hash = sha256_file(local_name)
            is_valid = (actual_hash == expected_hash)
            send_message(conn, {"verified": is_valid})
            ack = receive_message(conn)

            total_time = time.time() - start
            if total_time == 0:
                total_time = 0.001

            mb_size = (file_size - start_offset) / (1024 * 1024)
            rate = mb_size / total_time

            # Displays the download metrics
            if is_valid and ack.get("status") == "ok":
                print(f"\nSize: {file_size / (1024 * 1024):.2f} MB")
                print(f"Transfer Size: {total_time:.2f} seconds")
                print(f"Average Speed: {rate:.2f} MB/s")
                print(f"Response Time: {resp_time:.2f} ms")
                print(f"[OK] Download completed.")
            else:
                print("\n[ERROR] File integrity check failed.")

        # delete file
        elif cmd == "delete":
            # Error handling if no filename was provided
            if len(data) < 2:
                print("Error: missing filename")
                continue
            # Sends deletion request to the server
            send_message(conn, {
                "id": uuid.uuid4().hex,
                "op": "delete",
                "path": data[2]
            })
            res = receive_message(conn)
            if res.get("status") == "ok":
                print(f"[OK] Deleted {data[1]}")
            else:
                print(f"[ERROR] {res.get('message', 'File not found')}")

        # list directory contents
        elif cmd == "dir":
            subpath = data[1] if len(data) > 1 else ""
            send_message(conn, {
                "id": uuid.uuid4().hex,
                "op": "dir",
                "path": subpath
            })
            res = receive_message(conn)
            if res.get("status") == "ok":
                entries = res.get("entries", [])
                if len(entries) == 0:
                    print("Directory is empty.")
                else:
                    for item in entries:
                        if item["type"] == "dir":
                            print(f"[DIR] {item['name']}")
                        else:
                            print(f"[FILE] {item['name']} ({item['bytes']} bytes")
            else:
                print(f"[ERROR] {res.get('message', 'Directory error')}")

        # subfolder create and delete
        elif cmd == "subfolder":
            parts = user_input.split(" ", 2)
            if len(parts) <  3 or parts[1].lower() not in ("create", "delete"):
                print("Error: subfolder <create/delete> <path>")
                continue

            op_name = "mkdir" if parts[1].lower() == "create" else "rmdir"
            send_message(conn, {
                "id": uuid.uuid4().hex,
                "op": op_name,
                "path": parts[2]
            })
            res = receive_message(conn)
            if res.get("status") == "ok":
                print(f"[OK] Subfolder {parts[1].lower()}d: {parts[2]}")
            else:
                print(f"[ERROR] {res.get('message', 'Subfolder error')}")

        # disconnect
        elif cmd == "disconnect":
            try:
                send_message(conn, {"id": uuid.uuid4().hex, "op": "disconnect"})
                receive_message(conn)
            except (OSError, EOFError, ProtocolError):
                pass
            conn.close()
            print("[OK] Disconnected from server.")
            break

        else:
            print("Invalid command.")

if __name__ == "__main__":
    main()
