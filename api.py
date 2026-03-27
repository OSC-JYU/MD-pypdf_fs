from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import os
from dotenv import load_dotenv
import json
import uuid
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError, PyPdfError
import tempfile
import logging
import io
import shutil
from typing import Dict, List, Optional, Tuple, Any

from PIL import Image

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
MD_PATH_ENV = os.getenv("MD_PATH", "")
CONTAINER_MODE = os.getenv("CONTAINER", "").strip().lower() in ("1", "true", "yes", "on")
STORAGE_MODE = (os.getenv("STORAGE_MODE") or os.getenv("FILE_STORAGE_MODE") or "disk").strip().lower()

REQUEST_READ_CHUNK_SIZE = int(os.getenv("REQUEST_READ_CHUNK_SIZE", str(1024 * 1024)))

DEFAULT_IMAGE_MIN_WIDTH = int(os.getenv("PDF_IMAGE_MIN_WIDTH", "200"))
DEFAULT_IMAGE_MIN_HEIGHT = int(os.getenv("PDF_IMAGE_MIN_HEIGHT", "200"))
DEFAULT_IMAGE_MAX_ASPECT_RATIO = float(os.getenv("PDF_IMAGE_MAX_ASPECT_RATIO", "8.0"))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("md-pdf-splitter-fs")


def log_event(level: str, event: str, **fields):
    record = {"event": event, **fields}
    line = json.dumps(record, default=str)
    getattr(logger, level, logger.info)(line)


def resolve_md_root(md_path_env: str, container_mode: bool) -> str:
    """Resolve MessyDesk root that contains data/."""
    if STORAGE_MODE == "disk" and (not isinstance(md_path_env, str) or not md_path_env.strip()):
        raise RuntimeError("MD_PATH must be set when STORAGE_MODE=disk")

    candidates = []
    if isinstance(md_path_env, str) and md_path_env.strip():
        raw = os.path.abspath(md_path_env.strip())
        if os.path.basename(raw) == 'data':
            candidates.append(os.path.dirname(raw))
        candidates.append(raw)

    if container_mode:
        candidates.append('/app')

    candidates.append(os.path.abspath('.'))

    seen = set()
    existing_dirs = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if os.path.isdir(os.path.join(candidate, 'data')):
            return candidate
        if os.path.isdir(candidate):
            existing_dirs.append(candidate)

    if existing_dirs:
        return existing_dirs[0]

    raise RuntimeError(
        "Could not resolve MessyDesk data root. Set MD_PATH to the MessyDesk root "
        "(contains data/). If running in container, set CONTAINER=true and MD_PATH=/app."
    )


def resolve_md_relative_path(relative_path: str) -> str:
    """Resolve a MessyDesk relative path under MD_ROOT and block traversal/absolute input."""
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise HTTPException(400, "Invalid file.path")
    if os.path.isabs(relative_path):
        raise HTTPException(400, "file.path must be relative to MD_PATH")

    md_root = os.path.abspath(MD_ROOT)
    resolved = os.path.abspath(os.path.join(md_root, relative_path))
    if resolved != md_root and not resolved.startswith(md_root + os.sep):
        raise HTTPException(400, "file.path is outside MD_PATH")
    return resolved


def parse_request_payload(raw: bytes) -> Dict:
    try:
        payload = json.loads(raw.decode('utf-8'))
    except Exception:
        raise HTTPException(400, "Invalid JSON payload")

    if not isinstance(payload, dict):
        raise HTTPException(400, "Request payload must be a JSON object")
    if 'file' not in payload or not isinstance(payload['file'], dict):
        raise HTTPException(400, "Missing required fields: file")
    if 'path' not in payload['file']:
        raise HTTPException(400, "Missing required fields: file.path")
    return payload


def callback_enabled(request_json: dict) -> bool:
    return bool(
        isinstance(request_json.get('process'), dict)
        and isinstance(request_json.get('file'), dict)
        and request_json['file'].get('@rid')
        and request_json.get('userId')
        and request_json.get('output_set')
    )


def infer_output_metadata(output_path: str, default_type: str, default_extension: str) -> Tuple[str, str]:
    if default_type != "image":
        return default_type, default_extension

    ext = os.path.splitext(output_path)[1].lower().lstrip('.')
    if ext == 'jpeg':
        ext = 'jpg'
    if ext not in {'jpg', 'png'}:
        ext = 'png'
    return 'image', ext


def build_disk_response(request_json: dict, output_paths: List[str], default_type: str, default_extension: str) -> dict:
    source_path = str(request_json.get('file', {}).get('path', ''))
    db_name = 'messydesk'
    parts = source_path.replace('\\', '/').split('/')
    for idx, part in enumerate(parts[:-1]):
        if part == 'data' and idx + 1 < len(parts) and parts[idx + 1]:
            db_name = parts[idx + 1]
            break

    tmp_dir = os.path.join(MD_ROOT, 'data', db_name, 'tmp')
    os.makedirs(tmp_dir, exist_ok=True)

    files = []
    for output_path in output_paths:
        safe_name = os.path.basename(output_path)
        callback_name = safe_name
        if os.path.isfile(output_path):
            target_path = os.path.join(tmp_dir, callback_name)
            if os.path.abspath(output_path) != os.path.abspath(target_path):
                if os.path.exists(target_path):
                    callback_name = f"{uuid.uuid4().hex}_{safe_name}"
                    target_path = os.path.join(tmp_dir, callback_name)
                shutil.copy2(output_path, target_path)

        output_type, output_extension = infer_output_metadata(output_path, default_type, default_extension)
        files.append(
            {
                "path": callback_name,
                "label": safe_name,
                "type": output_type,
                "extension": output_extension,
            }
        )

    return {
        "task": request_json.get('task', {}).get('id'),
        "response": {
            "type": "disk",
            "files": files,
        },
    }


def parse_int_param(params: dict, key: str, default: int, minimum: int = 1) -> int:
    value = params.get(key, default)
    try:
        parsed = int(value)
        return parsed if parsed >= minimum else default
    except (TypeError, ValueError):
        return default


def parse_float_param(params: dict, key: str, default: float, minimum: float = 1.0) -> float:
    value = params.get(key, default)
    try:
        parsed = float(value)
        return parsed if parsed >= minimum else default
    except (TypeError, ValueError):
        return default


try:
    MD_ROOT = resolve_md_root(MD_PATH_ENV, CONTAINER_MODE)
except RuntimeError as err:
    print(f"ERROR: {err} \nexiting...")
    exit(1)

app = FastAPI(
    title="pypdf API",
    description="API for pypdf",
    version="1.0.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)


def fix_broken_pdf(input_path):
    """Fix a broken PDF by finding the first %PDF- marker and extracting from there."""
    with open(input_path, 'rb') as f:
        content = f.read()
    
    # Find the first occurrence of %PDF-
    pdf_marker = b'%PDF-'
    marker_pos = content.find(pdf_marker)
    
    if marker_pos == -1:
        raise ValueError("No PDF marker found in file")
    
    # Extract everything from the marker onwards
    fixed_content = content[marker_pos:]
    
    # Write to a temporary file
    temp_fd, temp_path = tempfile.mkstemp(suffix='.pdf')
    try:
        with os.fdopen(temp_fd, 'wb') as f:
            f.write(fixed_content)
        return temp_path
    except Exception:
        os.close(temp_fd)
        raise


def split_pdf_to_pages(input_file: str) -> Dict[str, Any]:
    temp_file = None
    input_dir = os.path.dirname(input_file)
    output_dir = os.path.join(input_dir, 'pages')
    os.makedirs(output_dir, exist_ok=True)

    try:
        try:
            reader = PdfReader(input_file)
            page_count = len(reader.pages)
        except (PdfReadError, Exception) as err:
            log_event("warning", "pdf_read_failed_try_fix", input_file=input_file, error=str(err))
            temp_file = fix_broken_pdf(input_file)
            reader = PdfReader(temp_file)
            page_count = len(reader.pages)

        successful_pages = 0
        page_paths: List[str] = []
        pad_width = max(3, len(str(page_count)))
        for i, page in enumerate(reader.pages, start=1):
            try:
                writer = PdfWriter()
                writer.add_page(page)
                output_path = os.path.join(output_dir, f"page_{i:0{pad_width}d}.pdf")
                with open(output_path, "wb") as f:
                    writer.write(f)
                successful_pages += 1
                page_paths.append(output_path)
            except (PyPdfError, IOError, OSError, Exception) as err:
                log_event("warning", "pdf_page_write_failed", page=i, error=str(err))

        return {
            "page_count": page_count,
            "successful_pages": successful_pages,
            "page_paths": page_paths,
        }
    finally:
        if temp_file and os.path.exists(temp_file):
            os.remove(temp_file)


def extract_text_from_pdf(input_file: str, source_label: Optional[str] = None) -> Dict[str, Any]:
    temp_file = None
    input_dir = os.path.dirname(input_file)
    output_dir = os.path.join(input_dir, 'pages')
    os.makedirs(output_dir, exist_ok=True)

    try:
        try:
            reader = PdfReader(input_file)
            page_count = len(reader.pages)
        except (PdfReadError, Exception) as err:
            log_event("warning", "pdf_read_failed_try_fix", input_file=input_file, error=str(err))
            temp_file = fix_broken_pdf(input_file)
            reader = PdfReader(temp_file)
            page_count = len(reader.pages)

        successful_pages = 0
        text_paths: List[str] = []
        # Prefer graph/node label over storage path basename, so labels remain user-meaningful.
        label_basename = os.path.basename(source_label) if isinstance(source_label, str) and source_label else os.path.basename(input_file)
        label_stem = os.path.splitext(label_basename)[0]
        for i, page in enumerate(reader.pages, start=1):
            try:
                page_text = page.extract_text() or ""
                if page_count == 1:
                    # Preserve split-page names such as page_12.pdf -> page_12.pdf.txt.
                    output_filename = f"{label_basename}.txt"
                else:
                    output_filename = f"{label_stem}_page_{i}.pdf.txt"
                output_path = os.path.join(output_dir, output_filename)
                with open(output_path, "w", encoding="utf-8") as handle:
                    handle.write(page_text)
                successful_pages += 1
                text_paths.append(output_path)
            except Exception as err:
                log_event("warning", "pdf_page_text_extract_failed", page=i, error=str(err))

        return {
            "page_count": page_count,
            "successful_pages": successful_pages,
            "text_paths": text_paths,
        }
    finally:
        if temp_file and os.path.exists(temp_file):
            os.remove(temp_file)


def extract_images_from_pdf(
    input_file: str,
    source_label: Optional[str] = None,
    min_width: int = DEFAULT_IMAGE_MIN_WIDTH,
    min_height: int = DEFAULT_IMAGE_MIN_HEIGHT,
    max_aspect_ratio: float = DEFAULT_IMAGE_MAX_ASPECT_RATIO,
) -> Dict[str, Any]:
    temp_file = None
    input_dir = os.path.dirname(input_file)
    output_dir = os.path.join(input_dir, 'pages')
    os.makedirs(output_dir, exist_ok=True)

    try:
        try:
            reader = PdfReader(input_file)
            page_count = len(reader.pages)
        except (PdfReadError, Exception) as err:
            log_event("warning", "pdf_read_failed_try_fix", input_file=input_file, error=str(err))
            temp_file = fix_broken_pdf(input_file)
            reader = PdfReader(temp_file)
            page_count = len(reader.pages)

        image_paths: List[str] = []
        successful_images = 0
        label_basename = os.path.basename(source_label) if isinstance(source_label, str) and source_label else os.path.basename(input_file)
        label_stem = os.path.splitext(label_basename)[0]

        for page_num, page in enumerate(reader.pages, start=1):
            page_images = getattr(page, "images", [])
            for image_index, image_file in enumerate(page_images, start=1):
                try:
                    image_name = getattr(image_file, "name", "") or ""
                    image_data = getattr(image_file, "data", None)
                    source_ext = os.path.splitext(image_name)[1].lower().lstrip('.')
                    if source_ext == 'jpeg':
                        source_ext = 'jpg'

                    output_ext = 'png'
                    output_bytes = None

                    pil_img = None
                    if isinstance(image_data, (bytes, bytearray)):
                        pil_img = Image.open(io.BytesIO(image_data))
                    else:
                        log_event("warning", "pdf_image_extract_failed", page=page_num, image_index=image_index, reason="no_image_data")
                        continue

                    width, height = pil_img.size
                    if width < min_width or height < min_height:
                        log_event(
                            "info",
                            "pdf_image_skipped_small",
                            page=page_num,
                            image_index=image_index,
                            width=width,
                            height=height,
                            min_width=min_width,
                            min_height=min_height,
                        )
                        continue

                    aspect_ratio = max(width / height, height / width)
                    if aspect_ratio > max_aspect_ratio:
                        log_event(
                            "info",
                            "pdf_image_skipped_aspect_ratio",
                            page=page_num,
                            image_index=image_index,
                            width=width,
                            height=height,
                            aspect_ratio=round(aspect_ratio, 3),
                            max_aspect_ratio=max_aspect_ratio,
                        )
                        continue

                    # Keep native jpg/png payload when possible for speed and fidelity.
                    if source_ext in {'jpg', 'png'}:
                        output_ext = source_ext
                        output_bytes = bytes(image_data)
                    else:
                        if pil_img.mode in ("RGBA", "LA", "P"):
                            pil_img = pil_img.convert("RGBA")
                        else:
                            pil_img = pil_img.convert("RGB")

                        buffer = io.BytesIO()
                        if pil_img.mode == "RGBA":
                            output_ext = 'png'
                            pil_img.save(buffer, format='PNG')
                        else:
                            output_ext = 'jpg'
                            pil_img.save(buffer, format='JPEG', quality=90)
                        output_bytes = buffer.getvalue()

                    if not output_bytes:
                        log_event("warning", "pdf_image_extract_failed", page=page_num, image_index=image_index, reason="no_image_bytes")
                        continue

                    output_filename = f"{label_stem}_page_{page_num}_image_{image_index}.{output_ext}"
                    output_path = os.path.join(output_dir, output_filename)
                    with open(output_path, 'wb') as handle:
                        handle.write(output_bytes)

                    successful_images += 1
                    image_paths.append(output_path)
                except Exception as err:
                    log_event("warning", "pdf_image_extract_failed", page=page_num, image_index=image_index, error=str(err))

        return {
            "page_count": page_count,
            "successful_pages": successful_images,
            "image_paths": image_paths,
        }
    finally:
        if temp_file and os.path.exists(temp_file):
            os.remove(temp_file)


@app.get("/")
async def root():
    return {"message": "pypdf API for MessyDesk"}

@app.post("/process")
async def process_files(
    message: UploadFile = File(...)
):
    input_file = None
    try:
        log_event("info", "process_start")

        # Read message JSON in-memory to avoid disk roundtrip overhead.
        request_chunks = []
        while True:
            chunk = await message.read(REQUEST_READ_CHUNK_SIZE)
            if not chunk:
                break
            request_chunks.append(chunk)
        request_json = parse_request_payload(b"".join(request_chunks))

        input_file = resolve_md_relative_path(request_json['file']['path'])
        file_rid = request_json.get('file', {}).get('@rid')
        process_rid = request_json.get('process', {}).get('@rid') if isinstance(request_json.get('process'), dict) else None
        log_event("info", "process_context", input_file=input_file, file_rid=file_rid, process_rid=process_rid)

        if not os.path.exists(input_file):
            raise HTTPException(404, "File not found")

        task = request_json.get('task', {})
        task_id = task.get('id', 'split') if isinstance(task, dict) else 'split'

        if task_id == 'split':
            result = split_pdf_to_pages(input_file)
            output_paths = result.get("page_paths", [])
            output_type = "pdf"
            output_extension = "pdf"
        elif task_id == 'extract_text':
            result = extract_text_from_pdf(input_file, request_json.get('file', {}).get('label'))
            output_paths = result.get("text_paths", [])
            output_type = "text"
            output_extension = "txt"
        elif task_id == 'extract_images':
            task_params = task.get('params', {}) if isinstance(task, dict) else {}
            if not isinstance(task_params, dict):
                task_params = {}

            min_width = parse_int_param(task_params, 'min_width', DEFAULT_IMAGE_MIN_WIDTH)
            min_height = parse_int_param(task_params, 'min_height', DEFAULT_IMAGE_MIN_HEIGHT)
            max_aspect_ratio = parse_float_param(task_params, 'max_aspect_ratio', DEFAULT_IMAGE_MAX_ASPECT_RATIO)

            result = extract_images_from_pdf(
                input_file,
                source_label=request_json.get('file', {}).get('label'),
                min_width=min_width,
                min_height=min_height,
                max_aspect_ratio=max_aspect_ratio,
            )
            output_paths = result.get("image_paths", [])
            output_type = "image"
            output_extension = "png"
        else:
            raise HTTPException(400, f"Unsupported task: {task_id}")

        log_event(
            "info",
            "process_summary",
            status="success",
            input_file=input_file,
            page_count=result["page_count"],
            successful_pages=result["successful_pages"],
        )
        response = build_disk_response(request_json, output_paths, output_type, output_extension)
        response["page_count"] = result["page_count"]
        response["successful_pages"] = result["successful_pages"]
        return response
    except HTTPException:
        raise
    except Exception as err:
        log_event("error", "process_failed", input_file=input_file, error=str(err))
        raise HTTPException(500, f"Processing failed: {str(err)}")


if __name__ == "__main__":
    import uvicorn
    log_event(
        "info",
        "service_start",
        md_path_env=MD_PATH_ENV,
        container_mode=CONTAINER_MODE,
        md_root=MD_ROOT,
    )
    uvicorn.run(app, host="0.0.0.0", port=9002)
