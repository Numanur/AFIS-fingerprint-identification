# main.py — ESP32 + R307 image modes: detect(3x) and cls(loop) WITH/WITHOUT person_id
# detect: asks person_id, sends 3 images (X-Person-Id present)
# cls   : no person_id, sends one image per placement (no X-Person-Id)

import network, time, gc, sys, uselect, usocket
from machine import UART
from pyfingerprint import PyFingerprint

# ====== CONFIG ======
SSID, PASSWORD = 'NMARS', '1112131415'
HOST, PORT, PATH = '192.168.1.109', 3000, '/upload-image'
BAUD, UART_TX, UART_RX = 57600, 17, 16
W, H = 256, 288
PACKED_LEN = (W * H) // 2          # 36,864 bytes
SENSOR_ADDRESS = 0xFFFFFFFF
SAMPLES_PER_PERSON = 3             # <-- changed from 10 to 3
QUIET_MS = 800

cls_counter = 1

# ====== Wi-Fi ======
def wifi_connect():
    wlan = network.WLAN(network.STA_IF); wlan.active(True)
    if not wlan.isconnected():
        print("Connecting to Wi-Fi…", end='')
        wlan.connect(SSID, PASSWORD)
        t0 = time.time()
        while not wlan.isconnected() and (time.time()-t0) < 20:
            print('.', end=''); time.sleep(0.4)
        print()
    if not wlan.isconnected(): raise RuntimeError("Wi-Fi failed")
    print("✔ Wi-Fi:", wlan.ifconfig()[0])

# ====== Minimal FPM packets over UART ======
STARTCODE = 0xEF01
PACKET_COMMAND, PACKET_ACK = 0x01, 0x07
PACKET_DATA, PACKET_DATA_END = 0x02, 0x08
CMD_DOWNLOADIMAGE = 0x0A

def _be16(n): return bytes([(n>>8)&0xFF, n&0xFF])

def _write_packet(uart, address, ptype, payload_bytes):
    plen = len(payload_bytes) + 2
    hdr  = _be16(STARTCODE) + address.to_bytes(4,'big') + bytes([ptype]) + _be16(plen)
    cs   = (ptype + hdr[-2] + hdr[-1] + sum(payload_bytes)) & 0xFFFF
    uart.write(hdr + payload_bytes + _be16(cs))

def _read_exact(uart, n, timeout_ms=3000):
    buf = bytearray(); t0=time.ticks_ms()
    while len(buf) < n:
        chunk = uart.read(n - len(buf))
        if chunk: buf.extend(chunk)
        else:
            if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms: return None
            time.sleep_ms(1)
    return bytes(buf)

def _read_packet(uart, timeout_ms=5000):
    hdr = _read_exact(uart, 9, timeout_ms)
    if not hdr: raise Exception("timeout header")
    if hdr[0] != (STARTCODE>>8) or hdr[1] != (STARTCODE&0xFF): raise Exception("bad startcode")
    ptype = hdr[6]
    plen  = (hdr[7]<<8) | hdr[8]
    rest  = _read_exact(uart, plen, timeout_ms)
    if not rest: raise Exception("timeout body")
    payload = rest[:-2]
    rxcs = (rest[-2]<<8) | rest[-1]
    calc = (ptype + hdr[7] + hdr[8] + sum(payload)) & 0xFFFF
    if rxcs != calc: raise Exception("bad checksum")
    return ptype, payload

# ====== Finger helpers ======
def ensure_idle(f, quiet_ms=QUIET_MS):
    start=None
    while True:
        if not f.readImage():
            if start is None: start=time.ticks_ms()
            if time.ticks_diff(time.ticks_ms(), start) >= quiet_ms: break
            time.sleep_ms(120)
        else:
            start=None; time.sleep_ms(150)

def wait_for_finger(f, poll_cmd=None):
    print("Place finger…")
    ensure_idle(f)
    while not f.readImage():
        if poll_cmd:
            c = poll_cmd()
            if c in ('stop', 'quit', 'exit'): return ('abort', c)
        time.sleep_ms(80)
    print("✔ Captured")
    return ('ok', None)

def wait_for_lift(f, timeout_ms=7000):
    print("Remove finger…")
    t0=time.ticks_ms()
    while True:
        if not f.readImage(): print("✔ Finger removed"); break
        if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms:
            print("⚠ Timeout"); break
        time.sleep_ms(150)

def flush_uart(uart):
    try:
        while uart.any(): uart.read()
    except Exception:
        pass

# ====== HTTP streaming (no big buffers) ======
def http_post_start(host, port, path, body_len, headers_dict):
    s = usocket.socket()
    ai = usocket.getaddrinfo(host, port, 0, usocket.SOCK_STREAM)[0][-1]
    s.connect(ai)
    lines = [
        "POST {} HTTP/1.1".format(path),
        "Host: {}:{}".format(host, port),
        "Content-Type: application/octet-stream",
        "Content-Length: {}".format(body_len),
    ]
    for k,v in headers_dict.items():
        lines.append("{}: {}".format(k, v))
    lines.append(""); lines.append("")
    s.send("\r\n".join(lines).encode())
    return s

def http_post_finish(sock):
    try:
        sock.settimeout(3)
        _ = sock.recv(200)
    except Exception:
        pass
    try:
        sock.close()
    except Exception:
        pass

def stream_upimage_to_http(uart, address, sock):
    _write_packet(uart, address, PACKET_COMMAND, bytes([CMD_DOWNLOADIMAGE]))
    ptype, payload = _read_packet(uart)
    if ptype != PACKET_ACK or not payload or payload[0] != 0x00:
        code = payload[0] if payload else -1
        raise Exception("UpImage NACK code=%02X" % code)
    sent = 0
    while True:
        ptype, payload = _read_packet(uart, timeout_ms=5000)
        if ptype not in (PACKET_DATA, PACKET_DATA_END):
            raise Exception("unexpected packet %02X" % ptype)
        sock.send(payload)
        sent += len(payload)
        if ptype == PACKET_DATA_END:
            break
    return sent

# ====== Non-blocking console ======
poll = uselect.poll()
poll.register(sys.stdin, uselect.POLLIN)
def read_cmd_nb():
    try:
        if poll.poll(0):
            line = sys.stdin.readline()
            if line: return line.strip().lower()
    except Exception:
        pass
    return None

def ask_person_id():
    while True:
        try:
            pid = int(input("person_id (int): ").strip())
            return pid
        except Exception:
            print("Invalid person_id. Please enter an integer.")

# ====== Modes ======
def detect_mode(uart, f):
    person_id = ask_person_id()
    print("DETECT mode: person_id={} — capture SAME finger {} times.".format(person_id, SAMPLES_PER_PERSON))
    collected = 0
    while collected < SAMPLES_PER_PERSON:
        status, cmd = wait_for_finger(f, poll_cmd=read_cmd_nb)
        if status == 'abort':
            print("↩ command:", cmd); return cmd

        flush_uart(uart)

        headers = {
            "X-Format": "packed4",
            "X-Width":  str(W),
            "X-Height": str(H),
            "X-Person-Id": str(person_id),
            "X-Mode": "detect",
            "X-Filename": "{}_{}".format(person_id, collected + 1),  # 1_1, 1_2, 1_3 ...
            #"X-Filename": "pid{}_detect_{:02d}_{}".format(person_id, collected+1, time.ticks_ms()),
        }
        sock = None
        try:
            sock = http_post_start(HOST, PORT, PATH, PACKED_LEN, headers)
            sent = stream_upimage_to_http(uart, SENSOR_ADDRESS, sock)
            http_post_finish(sock)
            print("Saved capture {}/{} ({} bytes).".format(collected+1, SAMPLES_PER_PERSON, sent))
        except Exception as e:
            if sock: http_post_finish(sock)
            print("Upload error:", e)
            wait_for_lift(f); gc.collect(); ensure_idle(f); flush_uart(uart)
            c = read_cmd_nb()
            if c in ('stop','quit','exit'): print("↩ command:", c); return c
            continue

        wait_for_lift(f)
        gc.collect(); ensure_idle(f); flush_uart(uart)
        collected += 1

        c = read_cmd_nb()
        if c in ('stop','quit','exit'):
            print("↩ command:", c); return c

    print("✅ DETECT complete: {} images captured for person_id={}.".format(SAMPLES_PER_PERSON, person_id))
    return None

def cls_mode(uart, f):
    global cls_counter
    print("CLS mode (predict): one capture per placement. Type 'stop' to exit.")
    while True:
        status, cmd = wait_for_finger(f, poll_cmd=read_cmd_nb)
        if status == 'abort':
            print("↩ command:", cmd); return cmd

        flush_uart(uart)

        headers = {
            "X-Format": "packed4",
            "X-Width":  str(W),
            "X-Height": str(H),
            "X-Mode": "cls",
            "X-Filename": "test_img_{}".format(cls_counter)
            # NOTE: no X-Person-Id on purpose
        }
        sock = None
        try:
            sock = http_post_start(HOST, PORT, PATH, PACKED_LEN, headers)
            sent = stream_upimage_to_http(uart, SENSOR_ADDRESS, sock)
            http_post_finish(sock)
            print("Sent {} ({} bytes). Next…".format(headers["X-Filename"], sent))
            cls_counter += 1
        except Exception as e:
            if sock: http_post_finish(sock)
            print("Upload error:", e)

        wait_for_lift(f)
        gc.collect(); ensure_idle(f); flush_uart(uart)

        c = read_cmd_nb()
        if c in ('stop','quit','exit'):
            print("↩ command:", c); return c


# ====== Boot ======
wifi_connect()
uart = UART(2, baudrate=BAUD, tx=UART_TX, rx=UART_RX, timeout=2000)
f = PyFingerprint(uart)
if not f.verifyPassword(): raise RuntimeError("Sensor not found or wrong password")
print("✔ Sensor OK. Commands: detect | cls | stop | quit")

# ====== Command loop ======
while True:
    try:
        cmd = input(">> ").strip().lower()
    except Exception:
        continue

    if cmd in ('quit','exit'):
        print("Bye!"); break

    if cmd == 'detect':
        res = detect_mode(uart, f)
        if res in ('quit','exit'): print("Bye!"); break
        continue

    if cmd == 'cls':
        res = cls_mode(uart, f)
        if res in ('quit','exit'): print("Bye!"); break
        continue

    if cmd == 'stop':
        print("No running mode to stop.")
        continue

    print("Unknown command:", cmd)
