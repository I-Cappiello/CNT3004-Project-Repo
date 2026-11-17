# The client portion of the server code. This will be used by both clients (in any test runs) to connect to the server host.

# Importing necessary packages.
import os
import socket
import time
import hashlib

# The maximum frame size and encoding format
SIZE = 65536
FORMAT = 'utf-8'

# main function
def main():
    client = None
    connected = False
    # Prompts the user to enter the connect command.
    print('To connect to the server, please enter the command \'connect <server IP Address> <server Port #>\'.')

    # The application repeatedly waits for a command to be entered until the "disconnect" command is given.
    while True:
        # Awaiting User Input
        user_input = input("> ").strip()
        if user_input == "":
            continue

        # Splits command string into an array
        data = user_input.split(" ")
        cmd = data[0].lower()

        # Connection Operation
        if cmd == "connect":
            if len(data) < 3:
                print("Error: please enter IP and port")
                continue

            # Compiles the address from the inputted IP address and port #
            ip = data[1]
            port = int(data[2])
            addr = (ip, port)

            # The client attempts to connect to the server
            try:
                client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                client.connect(addr)

                msg = client.recv(1024).decode(FORMAT)
                print("[OK] Connected to server!")

                # Upon a successful connection, the user is prompted to enter credentials.
                # The password is hashed before being sent.
                user = input("Username: ")
                pw = input("Password: ")
                hashed_pw = hashlib.sha256(pw.encode(FORMAT)).hexdigest()

                # The credentials are sent to the server side
                client.send(f"AUTH@{user}@{hashed_pw}".encode(FORMAT)).hexidigest()
                auth_msg = client.recv(1024).decode(FORMAT)

                # Upon a successful reply, the command list is presented.
                if "OK" in auth_msg:
                    print("[OK] Authentication successful!")
                    connected = True
                    print("\n========== COMMAND MENU ==========")
                    print("1. upload <file name>")
                    print("2. download")
                    print("3. delete")
                    print("4. dir")
                    print("5. subfolder create")
                    print("6. subfolder delete")
                    print("7. disconnect")
                    print("=====================================")
                # Upon an unsuccessful reply, the connection is closed.
                else:
                    print("[ERROR] Authentication failed")
                    client.close()
            # Should an exception be raised while attempting the connection, an error message is displayed.
            except OSError:
                print("[ERROR] Connection failed")

        # The following is displayed if another command is entered before the client connects to the server. 
        elif not connected:
            print("Please connect to the server first.")

        # Upload Operation.
        elif cmd == "upload":
            if len(data) < 2:
                print("Error: missing filename")
                continue

            filename = data[1]
            # Error handling for if the file cannot be found
            if not os.path.exists(filename):
                print("[ERROR] File not found")
                continue

            # The file is retrieved before the client sends an upload request to the server.
            # The response time is recorded.
            file_size = os.path.getsize(filename)
            t1 = time.time()
            client.send(f"UPLOAD@{filename}@{file_size}".encode(FORMAT))
            res = client.recv(1024).decode(FORMAT)
            resp_time = (time.time() - t1) * 1000

            # If the application receives a response that the file already exists,
            # a prompt is given, asking to overwrite the file.
            if "OVERWRITE" in res:
                ans = input("File exists. Overwrite? (y/n): ")
                client.send(ans.encode(FORMAT))
                if ans.lower() != "y":
                    print("Upload cancelled")
                    continue
                client.recv(1024)

            # The uploading of the file.
            print(f"Uploading {filename}...")
            start = time.time()
            sent = 0
            old_p = -1

            # The file is read piece-by-piece (in frames with a max size of SIZE)
            # Once the entirety of the file is sent over, the file is closed.
            f = open(filename, "rb")
            while True:
                bytes_read = f.read(SIZE)
                if not bytes_read:
                    break
                client.sendall(bytes_read)
                sent += len(bytes_read)
                p = int((sent / file_size) * 100)
                if p != old_p:
                    print(f"Progress: {p}%", end="\r")
                    old_p = p
            f.close()

            # Upload time calculation 
            end = time.time()
            total_time = end - start
            if total_time == 0:
                total_time = 0.001

            # Upload rate calculation.
            mb_size = file_size / (1024 * 1024)
            rate = mb_size / total_time

            # Displays upload metrics
            print(f"\nSize: {mb_size:.2f} MB")
            print(f"\nTransfer Time: {total_time:.2f} seconds")
            print(f"\nAverage Speed: {rate:.2f} MB/s")
            print(f"\nRespond Time: {resp_time:.2f} ms")

            # Tells the server the upload is complete
            done_msg = client.recv(1024).decode(FORMAT)
            if "@" in done_msg:
                print(done_msg.split("@")[1])
            else:
                print(done_msg)

        # Download Operation
        elif cmd == "download":
                if len(data) < 2:
                    print("Error: missing filename")
                    continue

                filename = data[1]

                t1 = time.time()
                client.send(f"DOWNLOAD@{filename}".encode(FORMAT))
                res = client.recv(1024).decode(FORMAT)
                resp_time = (time.time() - t1) * 1000
                
                res_data = res.split("@")
                if res_data[0] == "ERROR":
                    print(f"[ERROR] {res_data[1]}")
                    continue

                file_size = int(res_data[1])
                client.send("READ@OK".encode(FORMAT))

                print("Downloading {filename}...")
                start = time.time()
                received = 0
                old_p = -1

                f = open(filename, "wb")
                while received < file_size:
                    left = file_size - received
                    if left < SIZE:
                        chunk = client.recv(left)
                    else:
                        chunk = client.recv(SIZE)

                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
                    p = int((received / file_size) * 100)
                    if p != old_p:
                        print(f"Progress: {p}%", end="\r")
                        old_p = p
                f.close()

                end = time.time()
                total_time = end - start
                if total_time == 0:
                    total_time = 0.001

                mb_size = file_size / (1024 * 1024)
                rate = mb_size / total_time

                print(f"\nSize: {mb_size:.2f} MB")
                print(f"Transfer Size: {total_time} seconds")
                print(f"Average Size: {rate:.2f} MB/s")
                print(f"Response Time: {resp_time:.2f} ms")
                print(f"[OK] Download completed.")

        # delete file on server
        elif cmd == "delete":
            if len(data) < 2:
                print("Error: missing filename")
                continue

            filename = data[1]
            client.send(f"DELETE@{filename}".encode(FORMAT))
            res = client.recv(1024).decode(FORMAT)
            if "@" in res:
                print(res.split("@")[1])
            else:
                print(res)

        # list directory
        elif cmd == "dir":
            client.send("DIR@".encode(FORMAT))
            res = client.recv(4096).decode(FORMAT)
            if "@" in res:
                print(res.split("@"[1]))
            else: 
                print(res)
        
        # create or delete subfolder
        elif cmd == "subfolder":
            if len(data) < 3:
                print("Error: subfolder <create/delete> <path>")
                continue

            action = data[1].upper()
            path = data[2]
            client.send(f"SUBFOLDER@{action}@{path}".encode(FORMAT))
            res = client.recv(1024).decode(FORMAT)
            if "@" in res:
                print(res.split("@")[1])
            else:
                print(res)
            
        # disconnect from server
        elif cmd == "disconnect":
            client.send("LOGOUT@".encode(FORMAT))
            client.close()
            print("[OK] Disconnected from server.")
            break

        else:
            print("Invalid command.")

if __name__ == "__main__":
    main()









