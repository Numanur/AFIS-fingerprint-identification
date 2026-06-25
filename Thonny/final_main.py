# main.py
# ESP32 + R307 reliable fingerprint enrollment/detection over normal HTTP
#
# Laptop backend --USB serial JSON--> ESP32
# R307 --UART--> ESP32 reusable RAM buffer
# ESP32 --Wi-Fi HTTP POST--> Express server

import gc
import network
import sys
import time
import ujson
import usocket
from machine import Pin, UART
from pyfingerprint import PyFingerprint


# ========================= CONFIG =========================

FIRMWARE_VERSION = "R307_HTTP_V4_LOW_MEMORY"

SSID = "NMARS2"
PASSWORD = "1112131415"

HOST = "192.168.43.183"
PORT = 3000
UPLOAD_PATH = "/upload-image"

UART_ID = 2
SENSOR_BAUD = 57600
UART_TX_PIN = 17
UART_RX_PIN = 16

# Smaller UART buffers leave more RAM for Wi-Fi and TCP.
UART_RXBUF_PRIMARY = 8192
UART_RXBUF_FALLBACK = 4096

SENSOR_ADDRESS = 0xFFFFFFFF

DEFAULT_WIDTH = 256
DEFAULT_HEIGHT = 288
IMAGE_FORMAT = "packed4"

DEFAULT_IMAGE_BYTES = (
    DEFAULT_WIDTH * DEFAULT_HEIGHT + 1
) // 2

# One reusable image buffer is kept for every capture.
IMAGE_GROW_BYTES = 4096
MAX_IMAGE_BYTES = 64 * 1024

FINGERS = ("thumb", "index")
SAMPLES_PER_FINGER = 1

FINGER_QUIET_MS = 800
FINGER_LIFT_TIMEOUT_MS = 10000
SENSOR_SETTLE_MS = 150

SENSOR_PACKET_TIMEOUT_MS = 12000
SENSOR_DOWNLOAD_RETRIES = 2
SENSOR_RECOVERY_MAX_MS = 12000
MAX_SENSOR_PACKET_LENGTH = 4096

HTTP_CONNECT_TIMEOUT_S = 12
HTTP_UPLOAD_TIMEOUT_S = 30
HTTP_RESPONSE_TIMEOUT_S = 60

HTTP_RETRIES = 2
HTTP_SEND_CHUNK = 1024
MAX_RESPONSE_BYTES = 64 * 1024

GREEN_LED_PIN = 12
RED_LED_PIN = 13

STARTCODE = 0xEF01

PACKET_COMMAND = 0x01
PACKET_DATA = 0x02
PACKET_ACK = 0x07
PACKET_END = 0x08

CMD_DOWNLOAD_IMAGE = 0x0A


# ========================= LEDS =========================

led_green = Pin(
    GREEN_LED_PIN,
    Pin.OUT,
)

led_red = Pin(
    RED_LED_PIN,
    Pin.OUT,
)


def leds_off():
    led_green.value(0)
    led_red.value(0)


def led_ready():
    led_green.value(1)
    led_red.value(0)


def led_busy():
    leds_off()


def led_success():
    led_green.value(1)
    led_red.value(0)


def led_failure():
    led_green.value(0)
    led_red.value(1)


# ========================= GENERAL =========================

def send_json(data):
    try:
        print(
            ujson.dumps(data)
        )

    except Exception as exc:
        print(
            ujson.dumps(
                {
                    "status": "error",
                    "message": (
                        "JSON output failed: %s"
                        % str(exc)
                    ),
                }
            )
        )


def collect_memory():
    try:
        gc.collect()
        time.sleep_ms(10)
        gc.collect()

    except Exception:
        pass


def print_memory(label):
    try:
        gc.collect()

        print(
            "[MEM] %s free=%d allocated=%d"
            % (
                label,
                gc.mem_free(),
                gc.mem_alloc(),
            )
        )

    except Exception:
        pass


def close_socket(sock):
    if sock is not None:
        try:
            sock.close()

        except Exception:
            pass


collect_memory()


# ========================= WI-FI =========================

wlan = network.WLAN(
    network.STA_IF
)


def wifi_connect(
    force=False,
    timeout_s=20,
):
    wlan.active(True)

    if (
        wlan.isconnected()
        and not force
    ):
        return

    if force:
        try:
            wlan.disconnect()

        except Exception:
            pass

        time.sleep_ms(200)

    print(
        "[WIFI] Connecting",
        end="",
    )

    wlan.connect(
        SSID,
        PASSWORD,
    )

    started = time.ticks_ms()

    while not wlan.isconnected():
        elapsed = time.ticks_diff(
            time.ticks_ms(),
            started,
        )

        if elapsed >= timeout_s * 1000:
            print()

            raise OSError(
                "Wi-Fi connection timeout"
            )

        print(
            ".",
            end="",
        )

        time.sleep_ms(400)

    print()

    print(
        "[WIFI] Connected:",
        wlan.ifconfig()[0],
    )


def ensure_wifi():
    if not wlan.isconnected():
        wifi_connect(force=True)


# ========================= UART =========================

def create_uart():
    try:
        uart_obj = UART(
            UART_ID,
            baudrate=SENSOR_BAUD,
            tx=UART_TX_PIN,
            rx=UART_RX_PIN,
            timeout=50,
            rxbuf=UART_RXBUF_PRIMARY,
        )

        print(
            "[UART] RX buffer:",
            UART_RXBUF_PRIMARY,
        )

        return uart_obj

    except Exception as exc:
        print(
            "[UART] Primary RX buffer failed:",
            exc,
        )

        collect_memory()

        uart_obj = UART(
            UART_ID,
            baudrate=SENSOR_BAUD,
            tx=UART_TX_PIN,
            rx=UART_RX_PIN,
            timeout=50,
            rxbuf=UART_RXBUF_FALLBACK,
        )

        print(
            "[UART] RX buffer:",
            UART_RXBUF_FALLBACK,
        )

        return uart_obj


def drain_uart_until_quiet(
    uart,
    quiet_ms=300,
    max_wait_ms=SENSOR_RECOVERY_MAX_MS,
):
    started = time.ticks_ms()
    last_data = started
    drained = 0

    while (
        time.ticks_diff(
            time.ticks_ms(),
            started,
        )
        < max_wait_ms
    ):
        available = uart.any()

        if available:
            read_size = min(
                max(available, 1),
                1024,
            )

            chunk = uart.read(
                read_size
            )

            if chunk:
                drained += len(chunk)

            last_data = time.ticks_ms()

        else:
            quiet_duration = (
                time.ticks_diff(
                    time.ticks_ms(),
                    last_data,
                )
            )

            if quiet_duration >= quiet_ms:
                if drained:
                    print(
                        "[UART] Drained %d stale bytes"
                        % drained
                    )

                return True

            time.sleep_ms(5)

    print(
        "[UART] Drain timeout after "
        "%d stale bytes"
        % drained
    )

    return False


def uart_write_all(
    uart,
    data,
):
    view = memoryview(data)
    total = 0

    while total < len(view):
        written = uart.write(
            view[total:]
        )

        if (
            written is None
            or written <= 0
        ):
            raise OSError(
                "UART write failed after "
                "%d bytes"
                % total
            )

        total += written

    return total


def sensor_write_packet(
    uart,
    packet_type,
    payload,
):
    payload_length = (
        len(payload) + 2
    )

    packet = bytearray(
        9 + len(payload) + 2
    )

    packet[0] = (
        STARTCODE >> 8
    ) & 0xFF

    packet[1] = (
        STARTCODE
    ) & 0xFF

    packet[2] = (
        SENSOR_ADDRESS >> 24
    ) & 0xFF

    packet[3] = (
        SENSOR_ADDRESS >> 16
    ) & 0xFF

    packet[4] = (
        SENSOR_ADDRESS >> 8
    ) & 0xFF

    packet[5] = (
        SENSOR_ADDRESS
    ) & 0xFF

    packet[6] = packet_type

    packet[7] = (
        payload_length >> 8
    ) & 0xFF

    packet[8] = (
        payload_length
    ) & 0xFF

    if payload:
        packet[
            9:9 + len(payload)
        ] = payload

    checksum = (
        packet_type
        + packet[7]
        + packet[8]
        + sum(payload)
    ) & 0xFFFF

    packet[-2] = (
        checksum >> 8
    ) & 0xFF

    packet[-1] = (
        checksum
    ) & 0xFF

    uart_write_all(
        uart,
        packet,
    )


def uart_read_exact(
    uart,
    size,
    timeout_ms,
):
    data = bytearray(size)

    offset = 0
    started = time.ticks_ms()

    while offset < size:
        elapsed = time.ticks_diff(
            time.ticks_ms(),
            started,
        )

        if elapsed >= timeout_ms:
            return None

        chunk = uart.read(
            size - offset
        )

        if chunk:
            count = len(chunk)

            data[
                offset:offset + count
            ] = chunk

            offset += count

        else:
            time.sleep_ms(1)

    return data


def find_startcode(
    uart,
    timeout_ms,
):
    started = time.ticks_ms()
    previous = -1

    while (
        time.ticks_diff(
            time.ticks_ms(),
            started,
        )
        < timeout_ms
    ):
        current = uart.read(1)

        if not current:
            time.sleep_ms(1)
            continue

        value = current[0]

        if (
            previous == 0xEF
            and value == 0x01
        ):
            return True

        previous = value

    return False


def sensor_read_packet(
    uart,
    timeout_ms=SENSOR_PACKET_TIMEOUT_MS,
):
    if not find_startcode(
        uart,
        timeout_ms,
    ):
        raise OSError(
            "timeout waiting for packet start"
        )

    # address(4) + type(1) + length(2)
    header = uart_read_exact(
        uart,
        7,
        timeout_ms,
    )

    if header is None:
        raise OSError(
            "timeout header"
        )

    packet_type = header[4]
    length_high = header[5]
    length_low = header[6]

    packet_length = (
        length_high << 8
    ) | length_low

    if (
        packet_length < 2
        or packet_length
        > MAX_SENSOR_PACKET_LENGTH
    ):
        raise ValueError(
            "invalid sensor packet length %d"
            % packet_length
        )

    body = uart_read_exact(
        uart,
        packet_length,
        timeout_ms,
    )

    if body is None:
        raise OSError(
            "timeout body; expected %d bytes"
            % packet_length
        )

    payload_length = (
        packet_length - 2
    )

    received_checksum = (
        body[-2] << 8
    ) | body[-1]

    calculated_checksum = (
        packet_type
        + length_high
        + length_low
        + sum(
            memoryview(body)[
                :payload_length
            ]
        )
    ) & 0xFFFF

    if (
        received_checksum
        != calculated_checksum
    ):
        raise ValueError(
            "bad checksum: received "
            "0x%04X expected 0x%04X"
            % (
                received_checksum,
                calculated_checksum,
            )
        )

    return (
        packet_type,
        body,
        payload_length,
    )


# ========================= FINGER =========================

def ensure_finger_removed(
    fingerprint,
    quiet_ms=FINGER_QUIET_MS,
):
    quiet_started = None

    while True:
        finger_present = (
            fingerprint.readImage()
        )

        if not finger_present:
            if quiet_started is None:
                quiet_started = (
                    time.ticks_ms()
                )

            quiet_duration = (
                time.ticks_diff(
                    time.ticks_ms(),
                    quiet_started,
                )
            )

            if quiet_duration >= quiet_ms:
                return

        else:
            quiet_started = None

        time.sleep_ms(120)


def wait_for_finger(
    fingerprint,
):
    led_ready()

    print(
        "[SENSOR] Place finger..."
    )

    ensure_finger_removed(
        fingerprint
    )

    while not fingerprint.readImage():
        time.sleep_ms(80)

    print(
        "[SENSOR] Finger captured"
    )

    led_busy()


def wait_for_lift(
    fingerprint,
    timeout_ms=FINGER_LIFT_TIMEOUT_MS,
):
    print(
        "[SENSOR] Remove finger..."
    )

    started = time.ticks_ms()

    while True:
        if not fingerprint.readImage():
            print(
                "[SENSOR] Finger removed"
            )

            return True

        elapsed = time.ticks_diff(
            time.ticks_ms(),
            started,
        )

        if elapsed >= timeout_ms:
            print(
                "[SENSOR] Finger-removal timeout"
            )

            return False

        time.sleep_ms(150)


# ========================= REUSABLE IMAGE BUFFER =========================

image_buffer = None


def create_image_buffer():
    global image_buffer

    collect_memory()

    image_buffer = bytearray(
        DEFAULT_IMAGE_BYTES
    )

    print(
        "[IMAGE] Buffer capacity:",
        len(image_buffer),
    )

    print_memory(
        "after image buffer allocation"
    )


def ensure_image_capacity(
    required_size,
):
    global image_buffer

    if required_size > MAX_IMAGE_BYTES:
        raise MemoryError(
            "image exceeds limit: "
            "%d > %d"
            % (
                required_size,
                MAX_IMAGE_BYTES,
            )
        )

    if required_size <= len(image_buffer):
        return

    new_capacity = len(
        image_buffer
    )

    while new_capacity < required_size:
        new_capacity += (
            IMAGE_GROW_BYTES
        )

    if new_capacity > MAX_IMAGE_BYTES:
        new_capacity = (
            MAX_IMAGE_BYTES
        )

    extra_bytes = (
        new_capacity
        - len(image_buffer)
    )

    image_buffer.extend(
        bytearray(extra_bytes)
    )

    print(
        "[IMAGE] Buffer expanded to:",
        len(image_buffer),
    )


def download_image_once(
    uart,
):
    drain_uart_until_quiet(
        uart,
        quiet_ms=50,
        max_wait_ms=300,
    )

    sensor_write_packet(
        uart,
        PACKET_COMMAND,
        bytes(
            (
                CMD_DOWNLOAD_IMAGE,
            )
        ),
    )

    try:
        (
            packet_type,
            body,
            payload_length,
        ) = sensor_read_packet(
            uart,
            SENSOR_PACKET_TIMEOUT_MS,
        )

    except Exception as exc:
        raise OSError(
            "image ACK failed: %s"
            % str(exc)
        )

    if packet_type != PACKET_ACK:
        raise ValueError(
            "expected image ACK, got "
            "0x%02X"
            % packet_type
        )

    if (
        payload_length < 1
        or body[0] != 0x00
    ):
        if payload_length:
            error_code = body[0]
        else:
            error_code = 0xFF

        raise OSError(
            "UpImage rejected with code "
            "0x%02X"
            % error_code
        )

    used = 0
    packet_count = 0

    while True:
        try:
            (
                packet_type,
                body,
                payload_length,
            ) = sensor_read_packet(
                uart,
                SENSOR_PACKET_TIMEOUT_MS,
            )

        except Exception as exc:
            raise OSError(
                "image packet %d failed "
                "after %d bytes: %s"
                % (
                    packet_count + 1,
                    used,
                    str(exc),
                )
            )

        if packet_type not in (
            PACKET_DATA,
            PACKET_END,
        ):
            raise ValueError(
                "unexpected image packet "
                "0x%02X after %d bytes"
                % (
                    packet_type,
                    used,
                )
            )

        required_size = (
            used + payload_length
        )

        ensure_image_capacity(
            required_size
        )

        image_buffer[
            used:required_size
        ] = memoryview(body)[
            :payload_length
        ]

        used = required_size
        packet_count += 1

        if packet_type == PACKET_END:
            break

    if used <= 0:
        raise ValueError(
            "sensor returned an empty image"
        )

    print(
        "[SENSOR] Downloaded %d bytes "
        "in %d packets"
        % (
            used,
            packet_count,
        )
    )

    return used


def download_image(
    fingerprint,
    uart,
):
    last_error = None

    for attempt in range(
        1,
        SENSOR_DOWNLOAD_RETRIES + 1,
    ):
        try:
            if attempt > 1:
                print(
                    "[SENSOR] Retrying image "
                    "download (%d/%d)"
                    % (
                        attempt,
                        SENSOR_DOWNLOAD_RETRIES,
                    )
                )

            return download_image_once(
                uart
            )

        except Exception as exc:
            last_error = exc

            print(
                "[SENSOR] Download failed:",
                exc,
            )

            drain_uart_until_quiet(
                uart,
                quiet_ms=350,
                max_wait_ms=(
                    SENSOR_RECOVERY_MAX_MS
                ),
            )

            collect_memory()

            if (
                attempt
                < SENSOR_DOWNLOAD_RETRIES
            ):
                time.sleep_ms(200)

                verified = (
                    fingerprint.verifyPassword()
                )

                if not verified:
                    raise OSError(
                        "sensor resynchronization "
                        "returned false"
                    )

                print(
                    "[SENSOR] UART communication "
                    "resynchronized"
                )

                time.sleep_ms(100)

    raise last_error


def capture_image(
    fingerprint,
    uart,
):
    captured = False

    try:
        collect_memory()

        drain_uart_until_quiet(
            uart,
            quiet_ms=50,
            max_wait_ms=300,
        )

        wait_for_finger(
            fingerprint
        )

        captured = True

        time.sleep_ms(
            SENSOR_SETTLE_MS
        )

        return download_image(
            fingerprint,
            uart,
        )

    finally:
        if captured:
            try:
                wait_for_lift(
                    fingerprint
                )

            except Exception as exc:
                print(
                    "[SENSOR] Lift check failed:",
                    exc,
                )


# ========================= IMAGE METADATA =========================

def positive_int(
    value,
    name,
):
    try:
        result = int(value)

    except Exception:
        raise ValueError(
            "%s must be an integer"
            % name
        )

    if result <= 0:
        raise ValueError(
            "%s must be greater than zero"
            % name
        )

    return result


def resolve_dimensions(
    image_length,
    command,
):
    image_format = str(
        command.get(
            "format",
            IMAGE_FORMAT,
        )
    ).strip().lower()

    if image_format != "packed4":
        raise ValueError(
            "R307 image format must be packed4"
        )

    width = positive_int(
        command.get(
            "width",
            DEFAULT_WIDTH,
        ),
        "width",
    )

    height = positive_int(
        command.get(
            "height",
            DEFAULT_HEIGHT,
        ),
        "height",
    )

    expected_length = (
        width * height + 1
    ) // 2

    if expected_length == image_length:
        return (
            image_format,
            width,
            height,
        )

    total_pixels = (
        image_length * 2
    )

    if total_pixels % width == 0:
        height = (
            total_pixels // width
        )

        print(
            "[IMAGE] Inferred dimensions: "
            "%dx%d"
            % (
                width,
                height,
            )
        )

        return (
            image_format,
            width,
            height,
        )

    if total_pixels % height == 0:
        width = (
            total_pixels // height
        )

        print(
            "[IMAGE] Inferred dimensions: "
            "%dx%d"
            % (
                width,
                height,
            )
        )

        return (
            image_format,
            width,
            height,
        )

    raise ValueError(
        "%d packed4 bytes do not match "
        "width=%d or height=%d"
        % (
            image_length,
            width,
            height,
        )
    )


# ========================= HTTP =========================

def socket_send_all(
    sock,
    data,
    chunk_size=HTTP_SEND_CHUNK,
):
    view = memoryview(data)

    total = 0
    data_length = len(view)

    while total < data_length:
        chunk_end = min(
            total + chunk_size,
            data_length,
        )

        position = total

        while position < chunk_end:
            written = sock.send(
                view[
                    position:chunk_end
                ]
            )

            if (
                written is None
                or written <= 0
            ):
                raise OSError(
                    "TCP write stopped after "
                    "%d of %d bytes"
                    % (
                        position,
                        data_length,
                    )
                )

            position += written

        total = chunk_end

        # Allow the TCP/Wi-Fi stack to process data.
        time.sleep_ms(1)

    return total


def build_http_header(
    body_length,
    headers,
):
    lines = [
        "POST %s HTTP/1.1"
        % UPLOAD_PATH,

        "Host: %s:%d"
        % (
            HOST,
            PORT,
        ),

        "Content-Type: "
        "application/octet-stream",

        "Content-Length: %d"
        % body_length,

        "Connection: close",
    ]

    for key, value in headers.items():
        lines.append(
            "%s: %s"
            % (
                key,
                value,
            )
        )

    lines.extend(
        (
            "",
            "",
        )
    )

    return "\r\n".join(
        lines
    ).encode()


def receive_http_response(
    sock,
):
    sock.settimeout(
        HTTP_RESPONSE_TIMEOUT_S
    )

    received = bytearray()
    separator = b"\r\n\r\n"

    while separator not in received:
        chunk = sock.recv(512)

        if not chunk:
            raise OSError(
                "server closed before "
                "HTTP headers completed"
            )

        received.extend(chunk)

        if len(received) > 8192:
            raise MemoryError(
                "HTTP response headers too large"
            )

    (
        head,
        initial_body,
    ) = bytes(received).split(
        separator,
        1,
    )

    head_text = head.decode(
        "iso-8859-1",
        "ignore",
    )

    lines = head_text.split(
        "\r\n"
    )

    try:
        status_code = int(
            lines[0].split(
                " ",
                2,
            )[1]
        )

    except Exception:
        raise ValueError(
            "invalid HTTP status line: %s"
            % lines[0]
        )

    response_headers = {}

    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(
                ":",
                1,
            )

            response_headers[
                key.strip().lower()
            ] = value.strip()

    body = bytearray(
        initial_body
    )

    content_length = (
        response_headers.get(
            "content-length"
        )
    )

    if content_length is not None:
        expected = int(
            content_length
        )

        if expected > MAX_RESPONSE_BYTES:
            raise MemoryError(
                "HTTP response body too large"
            )

        while len(body) < expected:
            remaining = (
                expected - len(body)
            )

            chunk = sock.recv(
                min(
                    1024,
                    remaining,
                )
            )

            if not chunk:
                raise OSError(
                    "incomplete HTTP response body"
                )

            body.extend(chunk)

        if len(body) > expected:
            del body[expected:]

    else:
        while True:
            chunk = sock.recv(512)

            if not chunk:
                break

            body.extend(chunk)

            if (
                len(body)
                > MAX_RESPONSE_BYTES
            ):
                raise MemoryError(
                    "HTTP response body too large"
                )

    return (
        status_code,
        bytes(body),
    )


def http_post_once(
    body_length,
    headers,
):
    sock = None
    request_header = None
    image_view = None

    try:
        ensure_wifi()
        collect_memory()

        print_memory(
            "before socket"
        )

        print(
            "[HTTP] Connecting to %s:%d"
            % (
                HOST,
                PORT,
            )
        )

        sock = usocket.socket(
            usocket.AF_INET,
            usocket.SOCK_STREAM,
        )

        sock.settimeout(
            HTTP_CONNECT_TIMEOUT_S
        )

        sock.connect(
            (
                HOST,
                PORT,
            )
        )

        print(
            "[HTTP] Connected"
        )

        sock.settimeout(
            HTTP_UPLOAD_TIMEOUT_S
        )

        request_header = (
            build_http_header(
                body_length,
                headers,
            )
        )

        header_sent = (
            socket_send_all(
                sock,
                request_header,
                chunk_size=512,
            )
        )

        print(
            "[HTTP] Header sent:",
            header_sent,
        )

        image_view = memoryview(
            image_buffer
        )[:body_length]

        body_sent = (
            socket_send_all(
                sock,
                image_view,
                chunk_size=HTTP_SEND_CHUNK,
            )
        )

        if body_sent != body_length:
            raise OSError(
                "upload incomplete: "
                "%d of %d bytes"
                % (
                    body_sent,
                    body_length,
                )
            )

        print(
            "[HTTP] Uploaded %d bytes"
            % body_sent
        )

        return receive_http_response(
            sock
        )

    finally:
        image_view = None
        request_header = None

        close_socket(sock)

        sock = None

        collect_memory()

        # Allow lwIP to release socket resources.
        time.sleep_ms(400)


def http_post(
    body_length,
    headers,
):
    last_error = None

    for attempt in range(
        1,
        HTTP_RETRIES + 1,
    ):
        try:
            if attempt > 1:
                print(
                    "[HTTP] Retrying upload "
                    "(%d/%d)"
                    % (
                        attempt,
                        HTTP_RETRIES,
                    )
                )

            return http_post_once(
                body_length,
                headers,
            )

        except Exception as exc:
            last_error = exc

            print(
                "[HTTP] Attempt %d failed: %s"
                % (
                    attempt,
                    str(exc),
                )
            )

            print_memory(
                "after HTTP failure"
            )

            if attempt < HTTP_RETRIES:
                if not wlan.isconnected():
                    try:
                        wifi_connect(
                            force=True
                        )

                    except Exception as wifi_exc:
                        print(
                            "[WIFI] Reconnect failed:",
                            wifi_exc,
                        )

                # Give failed TCP resources time to clear.
                time.sleep_ms(1500)

                collect_memory()

    raise last_error


def upload_image(
    body_length,
    headers,
):
    (
        status_code,
        response_body,
    ) = http_post(
        body_length,
        headers,
    )

    if not (
        200 <= status_code < 300
    ):
        detail = response_body.decode(
            "utf-8",
            "ignore",
        )[:300]

        raise OSError(
            "server returned HTTP %d: %s"
            % (
                status_code,
                detail,
            )
        )

    return response_body


# ========================= COMMAND MODES =========================

def finish_cycle(
    uart,
):
    try:
        drain_uart_until_quiet(
            uart,
            quiet_ms=100,
            max_wait_ms=1000,
        )

    except Exception:
        pass

    collect_memory()


def enroll_mode(
    uart,
    fingerprint,
    person_id,
    command,
):
    person_id = str(
        person_id
    ).strip()

    if (
        not person_id
        or not person_id.isdigit()
    ):
        led_failure()

        send_json(
            {
                "status": "error",
                "action": "enroll",
                "message": (
                    "NID must contain digits only"
                ),
            }
        )

        return

    print(
        "[ENROLL] NID:",
        person_id,
    )

    for finger in FINGERS:
        for sample in range(
            1,
            SAMPLES_PER_FINGER + 1,
        ):
            try:
                print(
                    "[ENROLL] %s sample %d"
                    % (
                        finger,
                        sample,
                    )
                )

                image_length = (
                    capture_image(
                        fingerprint,
                        uart,
                    )
                )

                (
                    image_format,
                    width,
                    height,
                ) = resolve_dimensions(
                    image_length,
                    command,
                )

                filename = (
                    "%s_%s_%d"
                    % (
                        person_id,
                        finger,
                        sample,
                    )
                )

                headers = {
                    "X-Format": image_format,
                    "X-Width": str(width),
                    "X-Height": str(height),
                    "X-Actual-Bytes": str(
                        image_length
                    ),
                    "X-Person-Id": person_id,
                    "X-Mode": "enroll",
                    "X-Filename": filename,
                    "X-Identify": "0",
                }

                response = upload_image(
                    image_length,
                    headers,
                )

                print(
                    "[ENROLL] Saved %s: %s"
                    % (
                        filename,
                        response.decode(
                            "utf-8",
                            "ignore",
                        ),
                    )
                )

                led_success()

            except Exception as exc:
                led_failure()

                send_json(
                    {
                        "status": "error",
                        "action": "enroll",
                        "nid": person_id,
                        "finger": finger,
                        "sample": sample,
                        "message": str(exc),
                    }
                )

                return

            finally:
                finish_cycle(
                    uart
                )

    send_json(
        {
            "status": "success",
            "action": "enroll",
            "nid": person_id,
        }
    )


def detect_mode(
    uart,
    fingerprint,
    command,
):
    try:
        print(
            "[DETECT] Single capture"
        )

        image_length = capture_image(
            fingerprint,
            uart,
        )

        (
            image_format,
            width,
            height,
        ) = resolve_dimensions(
            image_length,
            command,
        )

        headers = {
            "X-Format": image_format,
            "X-Width": str(width),
            "X-Height": str(height),
            "X-Actual-Bytes": str(
                image_length
            ),
            "X-Mode": "cls",
            "X-Identify": "1",
            "X-Filename": "detect_img_1",
        }

        response = upload_image(
            image_length,
            headers,
        )

        response_text = (
            response.decode(
                "utf-8",
                "ignore",
            )
        )

        try:
            result = ujson.loads(
                response_text
            )

        except Exception:
            raise ValueError(
                "server returned invalid JSON: %s"
                % response_text
            )

        if result.get("ok") is False:
            raise OSError(
                result.get("detail")
                or result.get("error")
                or "AFIS identification failed"
            )

        match_id = result.get(
            "match_id"
        )

        if match_id is None:
            match_id = result.get(
                "matchId"
            )

        if match_id is None:
            match_id = result.get(
                "nid"
            )

        if (
            match_id is not None
            and str(match_id) != ""
        ):
            led_success()

            send_json(
                {
                    "status": "success",
                    "action": "detect",
                    "nid": match_id,
                    "score": result.get(
                        "score"
                    ),
                    "threshold": result.get(
                        "threshold"
                    ),
                }
            )

        else:
            led_failure()

            send_json(
                {
                    "status": "error",
                    "action": "detect",
                    "nid": None,
                    "message": (
                        "no fingerprint match"
                    ),
                    "score": result.get(
                        "score"
                    ),
                    "threshold": result.get(
                        "threshold"
                    ),
                }
            )

    except Exception as exc:
        led_failure()

        send_json(
            {
                "status": "error",
                "action": "detect",
                "message": str(exc),
            }
        )

    finally:
        finish_cycle(
            uart
        )


# ========================= BOOT =========================

print(
    "[BOOT] Firmware:",
    FIRMWARE_VERSION,
)

wifi_connect()

uart = create_uart()

drain_uart_until_quiet(
    uart,
    quiet_ms=100,
    max_wait_ms=1000,
)

fingerprint = PyFingerprint(
    uart,
    address=SENSOR_ADDRESS,
    img_w=DEFAULT_WIDTH,
    img_h=DEFAULT_HEIGHT,
    rx_timeout_ms=(
        SENSOR_PACKET_TIMEOUT_MS
    ),
)

if not fingerprint.verifyPassword():
    raise RuntimeError(
        "Fingerprint sensor not found "
        "or wrong password"
    )

print(
    "[BOOT] Sensor verified"
)

create_image_buffer()

print(
    '[BOOT] ENROLL: '
    '{"cmd":"ENROLL",'
    '"nid":"1000000052"}'
)

print(
    '[BOOT] DETECT: '
    '{"cmd":"DETECT"}'
)

led_ready()


# ========================= USB SERIAL COMMAND LOOP =========================

while True:
    try:
        line = sys.stdin.readline()

        if not line:
            time.sleep_ms(50)
            continue

        line = line.strip()

        if not line:
            continue

        try:
            command = ujson.loads(
                line
            )

        except Exception:
            send_json(
                {
                    "status": "error",
                    "message": "Invalid JSON",
                }
            )

            continue

        command_name = str(
            command.get(
                "cmd",
                "",
            )
        ).strip().upper()

        if command_name == "ENROLL":
            nid = command.get(
                "nid"
            )

            if (
                nid is None
                or str(nid).strip() == ""
            ):
                send_json(
                    {
                        "status": "error",
                        "action": "enroll",
                        "message": "Missing NID",
                    }
                )

            else:
                enroll_mode(
                    uart,
                    fingerprint,
                    nid,
                    command,
                )

        elif command_name == "DETECT":
            detect_mode(
                uart,
                fingerprint,
                command,
            )

        elif command_name == "FORMAT":
            send_json(
                {
                    "status": "error",
                    "action": "format",
                    "message": (
                        "FORMAT is not supported "
                        "on ESP32; use the server "
                        "AFIS API"
                    ),
                }
            )

        elif command_name in (
            "STOP",
            "QUIT",
            "EXIT",
        ):
            leds_off()

            send_json(
                {
                    "status": "success",
                    "action": (
                        command_name.lower()
                    ),
                }
            )

            break

        else:
            send_json(
                {
                    "status": "error",
                    "message": "Unknown command",
                }
            )

    except Exception as exc:
        led_failure()

        send_json(
            {
                "status": "error",
                "message": str(exc),
            }
        )

        finish_cycle(
            uart
        )

