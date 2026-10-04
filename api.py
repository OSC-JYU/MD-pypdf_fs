from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
import os
from dotenv import load_dotenv
import json
import uuid
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError, PyPdfError
import tempfile
import logging
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any


import md_storage

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
MD_PATH_ENV = os.getenv("MD_PATH", "")
# HTTP mode keeps outputs here until the consumer downloads them
OUTPUT_FOLDER = Path(os.getenv("OUTPUT_FOLDER", "./output")).resolve()
OUTPUT_MAX_AGE_SECONDS = int(os.getenv("OUTPUT_MAX_AGE_SECONDS", str(24 * 3600)))

REQUEST_READ_CHUNK_SIZE = int(os.getenv("REQUEST_READ_CHUNK_SIZE", str(1024 * 1024)))

SERVICE_DESCRIPTOR_PATH = Path(os.getenv("SERVICE_DESCRIPTOR_PATH", "./service.json")).resolve()
SERVICE_HELP_PATH = Path(os.getenv("SERVICE_HELP_PATH", "./help/index.md")).resolve()
SERVICE_HELP_FALLBACK_PATH = Path(os.getenv("SERVICE_HELP_FALLBACK_PATH", "./README.md")).resolve()
SERVICE_ID_OVERRIDE = os.getenv("SERVICE_ID")
SERVICE_NAME_OVERRIDE = os.getenv("SERVICE_NAME")
SERVICE_ADAPTER_OVERRIDE = os.getenv("SERVICE_ADAPTER")
SERVICE_LOCAL_URL_OVERRIDE = os.getenv("SERVICE_LOCAL_URL")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("md-pdf-splitter-fs")


def log_event(level: str, event: str, **fields):
    record = {"event": event, **fields}
    line = json.dumps(record, default=str)
    getattr(logger, level, logger.info)(line)


def resolve_md_relative_path(relative_path: str) -> str:
    """Resolve a MessyDesk relative path under MD_ROOT and block traversal/absolute input."""
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise HTTPException(400, "Invalid file.path")
    if not md_storage.DISK_MODE:
        raise HTTPException(400, "No file uploaded as 'content', and disk mode is off (MD_PATH not found)")
    if os.path.isabs(relative_path):
        raise HTTPException(400, "file.path must be relative to MD_PATH")

    md_root = os.path.abspath(MD_ROOT)
    resolved = os.path.abspath(os.path.join(md_root, relative_path))
    if resolved != md_root and not resolved.startswith(md_root + os.sep):
        raise HTTPException(400, "file.path is outside MD_PATH")
    return resolved


def parse_request_payload(raw: bytes, require_path: bool = True) -> Dict:
    try:
        payload = json.loads(raw.decode('utf-8'))
    except Exception:
        raise HTTPException(400, "Invalid JSON payload")

    if not isinstance(payload, dict):
        raise HTTPException(400, "Request payload must be a JSON object")
    if 'file' not in payload or not isinstance(payload['file'], dict):
        raise HTTPException(400, "Missing required fields: file")
    if require_path and 'path' not in payload['file']:
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


def resolve_tmp_dir(request_json: dict) -> str:
    source_path = str(request_json.get('file', {}).get('path', ''))
    db_name = 'messydesk'
    parts = source_path.replace('\\', '/').split('/')
    for idx, part in enumerate(parts[:-1]):
        if part == 'data' and idx + 1 < len(parts) and parts[idx + 1]:
            db_name = parts[idx + 1]
            break

    tmp_dir = os.path.join(MD_ROOT, 'data', db_name, 'tmp')
    os.makedirs(tmp_dir, exist_ok=True)
    return tmp_dir


def build_disk_response(request_json: dict, output_paths: List[str], default_type: str, default_extension: str) -> dict:
    tmp_dir = resolve_tmp_dir(request_json)

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
                # move, not copy: the work dir is removed after the response is built
                shutil.move(output_path, target_path)

        files.append(
            {
                "path": callback_name,
                "label": safe_name,
                "type": default_type,
                "extension": default_extension,
            }
        )

    return {
        "task": request_json.get('task', {}).get('id'),
        "response": {
            "type": "disk",
            "files": files,
        },
    }


def apply_service_descriptor_overrides(descriptor: Dict[str, Any]) -> Dict[str, Any]:
    overrides = {
        "id": SERVICE_ID_OVERRIDE,
        "name": SERVICE_NAME_OVERRIDE,
        # elg_fs in disk mode, elg in HTTP mode, unless SERVICE_ADAPTER says otherwise
        "adapter": SERVICE_ADAPTER_OVERRIDE or md_storage.storage_adapter(),
        "local_url": SERVICE_LOCAL_URL_OVERRIDE,
    }

    for key, value in overrides.items():
        if isinstance(value, str) and value.strip():
            descriptor[key] = value.strip()

    return descriptor


def load_service_descriptor() -> Dict[str, Any]:
    try:
        with SERVICE_DESCRIPTOR_PATH.open("r", encoding="utf-8") as handle:
            descriptor = json.load(handle)
    except FileNotFoundError as err:
        raise RuntimeError(f"Descriptor file not found: {SERVICE_DESCRIPTOR_PATH}") from err
    except json.JSONDecodeError as err:
        raise RuntimeError(f"Descriptor file is not valid JSON: {err}") from err
    except Exception as err:
        raise RuntimeError(f"Could not load service descriptor: {err}") from err

    if not isinstance(descriptor, dict):
        raise RuntimeError("Descriptor root must be a JSON object")

    return apply_service_descriptor_overrides(descriptor)


def load_help_markdown() -> str:
    candidates = [SERVICE_HELP_PATH, SERVICE_HELP_FALLBACK_PATH]
    for candidate in candidates:
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    raise RuntimeError("Help markdown file not found")


# None in HTTP mode (MD_PATH unset or without data/), see md_storage.py
MD_ROOT = str(md_storage.MD_ROOT) if md_storage.DISK_MODE else None

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


def split_pdf_to_pages(input_file: str, output_dir: str) -> Dict[str, Any]:
    """One single-page PDF per page. A page that cannot be written is skipped and listed in
    failed_pages; when no page can be written the split fails."""
    temp_file = None
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

        # the original is removed after the import, so each page keeps its document's metadata
        # (title, author, dates), which pdfinfo shows
        try:
            metadata = {k: v for k, v in (reader.metadata or {}).items() if isinstance(v, str)}
        except Exception:
            metadata = {}

        page_paths: List[str] = []
        failed_pages: List[Dict[str, Any]] = []
        pad_width = max(3, len(str(page_count)))
        for i in range(1, page_count + 1):
            try:
                writer = PdfWriter()
                writer.add_page(reader.pages[i - 1])
                if metadata:
                    writer.add_metadata(metadata)
                output_path = os.path.join(output_dir, f"page_{i:0{pad_width}d}.pdf")
                with open(output_path, "wb") as f:
                    writer.write(f)
                page_paths.append(output_path)
            except Exception as err:
                log_event("warning", "pdf_page_write_failed", page=i, error=str(err))
                failed_pages.append({"page": i, "error": str(err)[:300]})

        if page_count and not page_paths:
            raise PageSplitError(f"None of the {page_count} pages could be split: {failed_pages[0]['error']}")
        if not page_count:
            raise PageSplitError("The PDF has no pages")

        return {
            "page_count": page_count,
            "successful_pages": len(page_paths),
            "failed_pages": failed_pages,
            "page_paths": page_paths,
        }
    finally:
        if temp_file and os.path.exists(temp_file):
            os.remove(temp_file)


class PageSplitError(ValueError):
    """The PDF could be read but gave no pages; reported to the user as the job's error."""


@app.get("/")
async def root():
    return {"message": "pypdf API for MessyDesk"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "md-pypdf_fs"}


@app.get("/config")
async def config():
    try:
        return load_service_descriptor()
    except RuntimeError as err:
        raise HTTPException(500, str(err))


@app.get("/help", response_class=PlainTextResponse)
async def help_markdown():
    try:
        return load_help_markdown()
    except RuntimeError as err:
        raise HTTPException(404, str(err))

def build_http_response(output_id: str, output_paths: List[str]) -> dict:
    """HTTP mode: a list of plain uris, so the elg adapter keeps each file name as the label and
    types it by extension, as the disk response does."""
    return {
        "response": {
            "type": "stored",
            "uri": [f"/files/{output_id}/{os.path.basename(p)}" for p in output_paths],
        },
    }


def sweep_stale_outputs():
    """Remove HTTP outputs nobody downloaded (a failed consumer job leaves them behind)."""
    if not OUTPUT_FOLDER.is_dir():
        return
    limit = time.time() - OUTPUT_MAX_AGE_SECONDS
    for entry in OUTPUT_FOLDER.iterdir():
        try:
            if entry.is_dir() and entry.stat().st_mtime < limit:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            pass


@app.get("/files/{output_id}/{filename}")
async def serve_file(output_id: str, filename: str, background_tasks: BackgroundTasks):
    file_path = (OUTPUT_FOLDER / output_id / filename).resolve()
    if file_path.parent.parent != OUTPUT_FOLDER or not file_path.is_file():
        raise HTTPException(404, "File not found")

    def remove_file(path: Path):
        # each output is downloaded once; the dir goes with its last file
        try:
            path.unlink()
            if not any(path.parent.iterdir()):
                path.parent.rmdir()
        except OSError as err:
            log_event("error", "file_remove_failed", path=str(path), error=str(err))

    background_tasks.add_task(remove_file, file_path)
    return FileResponse(str(file_path), background=background_tasks)


@app.post("/process")
async def process_files(
    message: UploadFile = File(...),
    # with a content upload the PDF comes over HTTP and the outputs are served from /files;
    # without one it is read from MessyDesk's storage (disk mode, see md_storage.py)
    content: Optional[UploadFile] = File(None)
):
    input_file = None
    work_dir = None
    http_mode = content is not None
    output_id = None
    served_ok = False
    try:
        log_event("info", "process_start")

        # Read message JSON in-memory to avoid disk roundtrip overhead.
        request_chunks = []
        while True:
            chunk = await message.read(REQUEST_READ_CHUNK_SIZE)
            if not chunk:
                break
            request_chunks.append(chunk)
        request_json = parse_request_payload(b"".join(request_chunks), require_path=not http_mode)

        if http_mode:
            sweep_stale_outputs()
            output_id = uuid.uuid4().hex
            output_dir = OUTPUT_FOLDER / output_id
            output_dir.mkdir(parents=True)
            input_file = str(output_dir / ".input.pdf")
            with open(input_file, "wb") as handle:
                shutil.copyfileobj(content.file, handle, REQUEST_READ_CHUNK_SIZE)
        else:
            input_file = resolve_md_relative_path(request_json['file']['path'])
        file_rid = request_json.get('file', {}).get('@rid')
        process_rid = request_json.get('process', {}).get('@rid') if isinstance(request_json.get('process'), dict) else None
        log_event("info", "process_context", input_file=input_file, file_rid=file_rid, process_rid=process_rid)

        if not os.path.exists(input_file):
            raise HTTPException(404, "File not found")

        task = request_json.get('task', {})
        task_id = task.get('id', 'split') if isinstance(task, dict) else 'split'

        # Outputs are written to a per-request work dir inside MessyDesk's tmp/ (same file system,
        # so moving them to tmp/ is a rename) and the dir is removed afterwards. Writing next to
        # the source file left a second copy of every output in project storage.
        # In HTTP mode the outputs are written straight into the served output dir.
        work_dir = tempfile.mkdtemp(prefix='.pypdf_', dir=str(OUTPUT_FOLDER / output_id) if http_mode else resolve_tmp_dir(request_json))

        if task_id != 'split':
            raise HTTPException(400, f"Unsupported task: {task_id} (this service only splits PDFs)")
        result = await run_in_threadpool(split_pdf_to_pages, input_file, work_dir)
        output_paths = result["page_paths"]

        log_event(
            "info",
            "process_summary",
            status="success",
            input_file=input_file,
            page_count=result["page_count"],
            successful_pages=result["successful_pages"],
        )
        if http_mode:
            served = []
            for output_path in output_paths:
                target = OUTPUT_FOLDER / output_id / os.path.basename(output_path)
                shutil.move(output_path, str(target))
                served.append(str(target))
            os.unlink(input_file)
            response = build_http_response(output_id, served)
            served_ok = True
        else:
            response = build_disk_response(request_json, output_paths, "pdf", "pdf")
        # inside "response", so that the consumer passes them on to MessyDesk with every page
        response["response"]["page_count"] = result["page_count"]
        response["response"]["failed_pages"] = result["failed_pages"]
        return response
    except HTTPException:
        raise
    except PageSplitError as err:
        log_event("error", "process_failed", input_file=input_file, error=str(err))
        raise HTTPException(422, str(err))
    except Exception as err:
        log_event("error", "process_failed", input_file=input_file, error=str(err))
        raise HTTPException(500, f"Processing failed: {str(err)}")
    finally:
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
        if http_mode and output_id and not served_ok:
            # nothing of a failed run is served
            shutil.rmtree(OUTPUT_FOLDER / output_id, ignore_errors=True)


if __name__ == "__main__":
    import uvicorn
    log_event(
        "info",
        "service_start",
        md_path_env=MD_PATH_ENV,
        storage_mode=md_storage.describe_mode(),
    )
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "9002")))
