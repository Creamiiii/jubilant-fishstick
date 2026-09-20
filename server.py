from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
import base64
from datetime import datetime
from email import policy
from email.parser import BytesParser
from func import *
import html
import json
import logging
import mimetypes
import mysql.connector
import os
import re
import ref
import threading
import time
from urllib import error as urlerror
from urllib import request as urlrequest

from PIL import ExifTags, Image

# Listen on all network interfaces so other devices on the LAN can connect.
HOST = "0.0.0.0"
PORT = 8000
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
CAMERA_PAIR_WINDOW_SECONDS = 60
STEREO_BASELINE_METERS = 0.1
STEREO_FOCAL_LENGTH_PIXELS = 3285
STEREO_CENTER_X = 2016
STEREO_CENTER_Y = 1512
UPLOAD_DIR = Path(__file__).with_name("uploads")
LOG_FILE = Path(__file__).with_name("log.txt")
DOTENV_FILE = Path(__file__).with_name(".env")


def _load_dotenv(path):
    """Load simple KEY=VALUE entries without logging their values."""
    if not path.is_file():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip().strip("'\"")
            if name and value:
                os.environ.setdefault(name, value)
    except OSError as error:
        logging.getLogger(__name__).warning(
            "Could not read .env file: %s", error
        )


_load_dotenv(DOTENV_FILE)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
ALLOWED_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)
LOGGER = logging.getLogger(__name__)


class _PendingUpload:
    def __init__(self, image_path, content_type):
        self.image_path = image_path
        self.content_type = content_type
        self.analysis = None
        self.completed = threading.Event()


class _CameraPairer:
    def __init__(self):
        self._lock = threading.Lock()
        self._pending = {}

    def analyze(self, image_path, content_type, camera_id):
        if camera_id not in {"1", "2"}:
            LOGGER.warning(
                "Skipping image analysis for %s: invalid or missing cameraId=%r",
                image_path.name,
                camera_id or "(missing)",
            )
            return None

        other_camera_id = "2" if camera_id == "1" else "1"
        current = _PendingUpload(image_path, content_type)
        with self._lock:
            waiting = self._pending.pop(other_camera_id, None)
            if waiting is None:
                self._pending[camera_id] = current

        if waiting is None:
            if not current.completed.wait(CAMERA_PAIR_WINDOW_SECONDS):
                with self._lock:
                    if self._pending.get(camera_id) is current:
                        del self._pending[camera_id]
                LOGGER.info(
                    "Camera %s upload expired without a camera %s pair",
                    camera_id,
                    other_camera_id,
                )
                return None
            return current.analysis

        pair = [
            (waiting.image_path, waiting.content_type, other_camera_id),
            (image_path, content_type, camera_id),
        ]
        try:
            analysis = _analyze_image_with_llm(pair)
        finally:
            waiting.analysis = locals().get("analysis")
            waiting.completed.set()
        return analysis


CAMERA_PAIRER = _CameraPairer()
ANALYSIS_SAVE_LOCK = threading.Lock()
SAVED_ANALYSIS_IDS = set()


class ImageUploadHandler(BaseHTTPRequestHandler):
    # Record HTTP errors and avoid a second traceback when a client disconnects.
    def send_error(self, code, message=None, explain=None):
        LOGGER.warning(
            "HTTP error %s from %s: %s",
            code,
            self.client_address[0],
            message or self.responses.get(code, ("Unknown error",))[0],
        )
        try:
            super().send_error(code, message, explain)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            LOGGER.info("Client disconnected before HTTP error %s was sent", code)

    def do_GET(self):
        # Serve the browser upload form.
        if urlparse(self.path).path != "/":
            self.send_error(404, "Not found")
            return

        body = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Image upload</title></head>
<body>
  <h1>Upload an image</h1>
  <form action="/upload" method="post" enctype="multipart/form-data">
    <input type="file" name="image" accept="image/*" required>
    <button type="submit">Upload</button>
  </form>
</body>
</html>"""
        self._send_html(body)

    def do_POST(self):
        # Route multipart and raw image requests to their upload handlers.
        path = urlparse(self.path).path
        if path == "/upload/raw":
            self._handle_raw_upload()
            return
        if path != "/upload":
            self.send_error(404, "Not found")
            return

        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type == "application/octet-stream" or content_type in ALLOWED_TYPES:
            self._handle_raw_upload()
            return

        transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()
        is_chunked = "chunked" in {
            value.strip() for value in transfer_encoding.split(",")
        }

        if is_chunked:
            try:
                request_body = self._read_chunked_body()
            except ValueError as error:
                self.send_error(400, str(error))
                return
            request_size = len(request_body)
            request_stream = BytesIO(request_body)
            request_headers = self.headers.__class__()
            for header_name, header_value in self.headers.items():
                request_headers[header_name] = header_value
            request_headers["Content-Length"] = str(request_size)
            del request_headers["Transfer-Encoding"]
        else:
            content_length = self.headers.get("Content-Length")
            try:
                request_size = int(content_length or "0")
            except ValueError:
                self.send_error(400, "Invalid Content-Length")
                return
            request_stream = self.rfile
            request_headers = self.headers

        if request_size <= 0:
            received_headers = " | ".join(
                f"{header_name}: {header_value}".replace("\r", "\\r").replace("\n", "\\n")
                for header_name, header_value in self.headers.items()
            )
            self.send_error(
                413,
                f"Request size {request_size} bytes is invalid; "
                f"Headers: {received_headers or '(none)'}",
            )
            return

        if request_size > MAX_UPLOAD_BYTES:
            self.send_error(
                413,
                f"Request size {request_size} bytes exceeds "
                f"MAX_UPLOAD_BYTES {MAX_UPLOAD_BYTES} bytes",
            )
            return

        if transfer_encoding and not is_chunked:
            self.send_error(501, "Only Transfer-Encoding: chunked is supported")
            return

        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self.send_error(400, "Expected multipart/form-data")
            return

        body = request_stream.read(request_size)
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: "
            + content_type.encode("ascii", "strict")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + body
        )
        image = next(
            (
                part
                for part in message.iter_attachments()
                if part.get_param("name", header="content-disposition") == "image"
            ),
            None,
        )
        if image is None or not image.get_filename():
            self.send_error(400, "Missing image field")
            return

        image_type = image.get_content_type()
        extension = ALLOWED_TYPES.get(image_type)
        if extension is None:
            self.send_error(415, "Only JPEG, PNG, GIF, and WebP images are supported")
            return

        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(image.get_filename()).name)
        safe_name = safe_name or "image"
        target = UPLOAD_DIR / f"{self._unique_prefix()}_{safe_name}"
        UPLOAD_DIR.mkdir(exist_ok=True)
        with target.open("wb") as output:
            output.write(image.get_payload(decode=True) or b"")

        message = f"Saved {html.escape(target.name)} ({html.escape(image_type)})"
        self._report_upload_info(target, image_type)
        objects = CAMERA_PAIRER.analyze(
            target, image_type, self.headers.get("cameraId", "")
        )
        analysis = _format_analysis(objects)
        self._send_html(
            f"<!doctype html><html><body><p>{message}</p>{analysis}"
            '<p><a href="/">Upload another image</a></p></body></html>'
        )

    def _handle_raw_upload(self):
        # Save a raw image body supplied with an image Content-Type.
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].lower()
        filename = self.headers.get("X-Filename", "")
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name)
        if not safe_name:
            safe_name = "image"

        if content_type == "application/octet-stream":
            content_type = self.headers.get("X-Content-Type", "").lower()
            if not content_type:
                content_type = mimetypes.guess_type(safe_name)[0] or ""
        extension = ALLOWED_TYPES.get(content_type)
        if extension is None:
            self.send_error(415, "Use a supported image Content-Type")
            return
        if not filename:
            safe_name = f"image{extension}"

        transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()
        is_chunked = "chunked" in {
            value.strip() for value in transfer_encoding.split(",")
        }
        if is_chunked:
            try:
                body = self._read_chunked_body()
            except ValueError as error:
                self.send_error(400, str(error))
                return
            request_size = len(body)
            request_stream = BytesIO(body)
        else:
            content_length = self.headers.get("Content-Length")
            try:
                request_size = int(content_length or "0")
            except ValueError:
                self.send_error(400, "Invalid Content-Length")
                return
            request_stream = self.rfile

        if request_size <= 0:
            self.send_error(413, f"Request size {request_size} bytes is invalid")
            return
        if request_size > MAX_UPLOAD_BYTES:
            self.send_error(
                413,
                f"Request size {request_size} bytes exceeds "
                f"MAX_UPLOAD_BYTES {MAX_UPLOAD_BYTES} bytes",
            )
            return

        target = UPLOAD_DIR / f"{self._unique_prefix()}_{safe_name}"
        UPLOAD_DIR.mkdir(exist_ok=True)
        with target.open("wb") as output:
            remaining = request_size
            while remaining:
                chunk = request_stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    self.send_error(400, "Incomplete request body")
                    return
                output.write(chunk)
                remaining -= len(chunk)

        self._report_upload_info(target, content_type)
        objects = CAMERA_PAIRER.analyze(
            target, content_type, self.headers.get("cameraId", "")
        )
        analysis = _format_analysis(objects)
        self._send_html(
            f"<!doctype html><html><body><p>Saved {html.escape(target.name)} "
            f"({html.escape(content_type)})</p>{analysis}</body></html>"
        )

    def _report_upload_info(self, image_path, content_type):
        # Print cameraId and useful image metadata after the file is fully saved.
        camera_header = next(
            (
                (header_name, header_value)
                for header_name, header_value in self.headers.items()
                if header_name.lower() == "cameraid"
            ),
            None,
        )
        camera_text = (
            f"{camera_header[0]}={camera_header[1]}"
            if camera_header
            else "cameraId=(not provided)"
        )

        capture_time = "(not available)"
        try:
            with Image.open(image_path) as image:
                exif = image.getexif()
                exif_names = {
                    ExifTags.TAGS.get(tag_id, tag_id): value
                    for tag_id, value in exif.items()
                }
                capture_time = (
                    exif_names.get("DateTimeOriginal")
                    or exif_names.get("DateTimeDigitized")
                    or exif_names.get("DateTime")
                    or "(not available)"
                )
                image_format = image.format or content_type
                image_dimensions = f"{image.width}x{image.height}"
        except (OSError, ValueError) as error:
            image_format = content_type
            image_dimensions = "(unavailable)"
            LOGGER.warning("Could not read image metadata for %s: %s", image_path.name, error)

        info = (
            f"Upload info: {camera_text}; file={image_path.name}; "
            f"type={image_format}; size={image_path.stat().st_size} bytes; "
            f"dimensions={image_dimensions}; capture_time={capture_time}"
        )
        print(info)
        LOGGER.info(info)


    def _send_html(self, body):
        # Send a small HTML response to the browser or upload client.
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _read_chunked_body(self):
        # Decode chunked transfer encoding while enforcing the upload size limit.
        body = bytearray()
        while True:
            size_line = self.rfile.readline(8192)
            if not size_line:
                raise ValueError("Incomplete chunked request")
            try:
                size_text = size_line.strip().split(b";", 1)[0]
                chunk_size = int(size_text, 16)
            except ValueError as error:
                raise ValueError("Invalid chunk size") from error

            if chunk_size == 0:
                while True:
                    trailer = self.rfile.readline(8192)
                    if not trailer or trailer in (b"\r\n", b"\n"):
                        return bytes(body)

            if len(body) + chunk_size > MAX_UPLOAD_BYTES:
                self.close_connection = True
                raise ValueError(
                    f"Request size exceeds MAX_UPLOAD_BYTES {MAX_UPLOAD_BYTES} bytes"
                )

            chunk = self.rfile.read(chunk_size)
            if len(chunk) != chunk_size:
                raise ValueError("Incomplete chunked request")
            if self.rfile.read(2) != b"\r\n":
                raise ValueError("Invalid chunk terminator")
            body.extend(chunk)

    @staticmethod
    def _unique_prefix():
        return f"{os.getpid()}_{int(__import__('time').time() * 1000)}"


def _analyze_image_with_llm(image_path, content_type=None):
    """Ask Gemini for stereo object measurements from one or two images."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        LOGGER.info("Skipping image analysis: GEMINI_API_KEY is not configured")
        return None

    try:
        if isinstance(image_path, list):
            images = image_path
        else:
            images = [(image_path, content_type, None)]
        image_parts = []
        for path, mime_type, camera_id in images:
            label = f" from camera {camera_id}" if camera_id else ""
            image_parts.extend(
                [
                    {"text": f"Image{label}:"},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                        }
                    },
                ]
            )
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": (
                                "Analyze the two stereo images as a calibrated pair. "
                                "The image labeled camera 1 is Left and camera 2 is Right. "
                                "Both images are 4032 x 3024 pixels. Use these parameters: "
                                "baseline B=0.1 m; focal length f=3285 px; principal point "
                                "(cx, cy)=(2016, 1512) px; Left camera center is "
                                "(-0.05, 0, 0) m and Right camera center is (+0.05, 0, 0) m. "
                                "Use a world coordinate system whose origin is the midpoint "
                                "between cameras, with +Z forward from the cameras, +X toward "
                                "the Right camera, and +Y downward in the image. "
                                "For each distinct object visible in both images, estimate its "
                                "color, center pixel in each image, approximate physical width "
                                "and height in meters, and 3D center coordinates in meters. "
                                "Use disparity d=x_left-x_right and these formulas: "
                                "Z=f*B/d, X=(x_left-cx)*Z/f-B/2, "
                                "Y=(y_left-cy)*Z/f. Estimate dimensions from the pixel "
                                "bounding box and depth when possible. Do not invent precise "
                                "values when the object is occluded, textureless, or has no "
                                "reliable match; use null and explain the limitation. "
                                "Return only valid JSON in exactly this form: "
                                '{"objects":[{"name":"...","color":"...",'
                                '"center_pixel_left":{"x":0,"y":0},'
                                '"center_pixel_right":{"x":0,"y":0},'
                                '"size_m":{"width":0,"height":0},'
                                '"coordinates_m":{"X":0,"Y":0,"Z":0},'
                                '"confidence":"high|medium|low",'
                                '"notes":"..."}]}. '
                                "Use null for unavailable numeric values."
                            )
                        },
                        *image_parts,
                    ]
                },
            ],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
            },
        }
        encoded_payload = json.dumps(payload).encode("utf-8")
        request = urlrequest.Request(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent",
            data=encoded_payload,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            method="POST",
        )
        retryable_statuses = {429, 500, 502, 503, 504}
        image_name = _analysis_image_name(image_path)
        for attempt in range(3):
            try:
                with urlrequest.urlopen(request, timeout=60) as response:
                    result = json.loads(response.read().decode("utf-8"))
                break
            except urlerror.HTTPError as error:
                if error.code not in retryable_statuses or attempt == 2:
                    raise
                delay = 2**attempt
                LOGGER.warning(
                    "Gemini returned HTTP %s for %s; retrying in %s seconds",
                    error.code,
                    image_name,
                    delay,
                )
                time.sleep(delay)
        content = result["candidates"][0]["content"]["parts"][0]["text"]
        response_data = _parse_llm_json(content)
        objects = response_data.get("objects")
        if not isinstance(objects, list) or not all(
            isinstance(item, dict) and isinstance(item.get("name"), str)
            for item in objects
        ):
            raise ValueError("LLM response did not contain a measured object list")
        LOGGER.info("Image analysis completed for %s: %s", image_name, objects)
        return objects
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        urlerror.URLError,
    ) as error:
        image_name = _analysis_image_name(image_path)
        LOGGER.warning("Could not analyze image %s: %s", image_name, error)
        return None


def _parse_llm_json(content):
    """Parse the first JSON object when Gemini adds fences or trailing text."""
    decoder = json.JSONDecoder()
    start = content.find("{")
    if start < 0:
        raise ValueError("LLM response did not contain a JSON object")
    parsed, _ = decoder.raw_decode(content[start:])
    if not isinstance(parsed, dict):
        raise ValueError("LLM response JSON was not an object")
    return parsed


def _analysis_image_name(image_path):
    if isinstance(image_path, list):
        return ", ".join(path.name for path, _, _ in image_path)
    return image_path.name


def _format_analysis(objects):
    if objects is None:
        return "<p>Object analysis unavailable. Gemini may be temporarily unavailable.</p>"
    if not objects:
        return "<p>Objects detected: none</p>"
    analysis_id = id(objects)
    with ANALYSIS_SAVE_LOCK:
        if analysis_id not in SAVED_ANALYSIS_IDS:
            conn = None
            try:
                conn = mysql.connector.connect(
                    host=ref.host,
                    user=ref.user,
                    password=ref.password,
                    database=ref.database,
                )
                cursor = conn.cursor()
                analysis_time = datetime.now()
                for item in objects:
                    left_pixel = item["center_pixel_left"]
                    right_pixel = item["center_pixel_right"]
                    size = item["size_m"]
                    coordinates = item["coordinates_m"]
                    insert_log(
                        cursor,
                        item["name"],
                        analysis_time,
                        item["color"],
                        left_pixel["x"],
                        left_pixel["y"],
                        right_pixel["x"],
                        right_pixel["y"],
                        size["width"],
                        size["height"],
                        coordinates["X"],
                        coordinates["Y"],
                        coordinates["Z"],
                        item["confidence"],
                        item.get("notes"),
                    )
                conn.commit()
                SAVED_ANALYSIS_IDS.add(analysis_id)
                LOGGER.info("Saved %s analyzed object(s) to database", len(objects))
            except (mysql.connector.Error, KeyError, TypeError, ValueError) as error:
                if conn is not None:
                    conn.rollback()
                LOGGER.warning("Could not save image analysis to database: %s", error)
            finally:
                if conn is not None and conn.is_connected():
                    conn.close()

    items = "".join(
        f"<li><b>{html.escape(item['name'])}</b>; "
        f"color: {html.escape(str(item.get('color') or 'unknown'))}; "
        f"size: {html.escape(json.dumps(item.get('size_m')))} m; "
        f"coordinates (X, Y, Z): "
        f"{html.escape(json.dumps(item.get('coordinates_m')))} m; "
        f"confidence: {html.escape(str(item.get('confidence') or 'unknown'))}; "
        f"{html.escape(str(item.get('notes') or ''))}</li>"
        for item in objects
    )
    return f"<p>Objects detected:</p><ul>{items}</ul>"


class ImageUploadServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        LOGGER.exception("Unhandled server error from %s", client_address[0])


if __name__ == "__main__":
    server = ImageUploadServer((HOST, PORT), ImageUploadHandler)
    print(f"Image server listening at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server")
    finally:
        server.server_close()
