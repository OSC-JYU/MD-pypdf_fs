
# MD-pdf-splitter_fs

An experimental MessyDesk wrapper for:
https://github.com/py-pdf/pypdf

The purpose of this service is to split a PDF into one-page PDF files.
It reads and writes directly under MessyDesk file storage (`_fs`).

## Tasks

- `split`: split a PDF into one-page PDF files under `pages/page_N.pdf`.
- `extract_text`: extract each page text to `pages/<pdf_label>_page_N.pdf.txt`.
	- Single-page input keeps original page label, for example `page_001.pdf` -> `page_001.pdf.txt`.
- `extract_images`: extract embedded PDF images to `pages/<pdf_label>_page_N_image_M.jpg|png`.
	- Small or extreme-aspect-ratio images are skipped by default.

### extract_images defaults

Default filtering is enabled to ignore likely noise images:

- `min_width = 200`
- `min_height = 200`
- `max_aspect_ratio = 8.0` where aspect ratio is `max(width/height, height/width)`

Image output format:

- Native embedded `jpg/png` is kept when possible.
- Other embedded formats are converted to `jpg` or `png`.

Task params can override defaults via queue message `task.params`:

```json
{
	"task": {
		"id": "extract_images",
		"params": {
			"min_width": 200,
			"min_height": 200,
			"max_aspect_ratio": 8.0
		}
	}
}
```

## API

Endpoint:

- `http://localhost:9002/process`

Payload is queue message as multipart file field `message` containing JSON.

## Running as service

Create `.env` file with:

	MD_PATH="/home/YOUR_USERNAME/Projects/MessyDesk"
	MD_URL="http://localhost:8200"

Service loads `.env` automatically on startup.

### Run with python

	python api.py

### Run with docker/podman

Build and start:

	make build
	make start

Or directly:

	docker run --name md-pdf-splitter_fs -p 9002:9002 \
	  -e MD_URL=http://host.containers.internal:8200 \
	  -v [MESSYDESK-PATH]/data/:/app/data:Z \
	  -e CONTAINER=true \
	  -e MD_PATH=/app \
	  messydesk/md-pdf-splitter_fs:0.1

## Adapter (MD-consumer)


	TOPIC=md-pypdf_fs DEV_URL=http://localhost:9002 node src/index.mjs

### Example API call

Run these from `MD-pdf-splitter_fs` directory:

	curl -X POST -H "Content-Type: multipart/form-data" \
	-F "message=@test/split.json;type=application/json" \
	  http://localhost:9002/process

## Config

Required:

- `MD_PATH`: path to MessyDesk root (directory that contains `data/`)
	- local Python run (host): `MD_PATH=/home/<user>/Projects/MessyDesk`
	- container run: `MD_PATH=/app`
- `CONTAINER`: set `CONTAINER=true` when running in container

Important:
- When `STORAGE_MODE=disk` (or `FILE_STORAGE_MODE=disk`), `MD_PATH` must be set.
- Disk responses use filename-only `response.files[].path` values (no absolute path).

Path notes:

- Service resolves runtime root from `MD_PATH` and validates it contains `data/`.
- Container mode is explicit; no container auto-detection heuristics are used.

Optional:

- `MD_URL` (default `http://localhost:8200`)
- `REQUEST_READ_CHUNK_SIZE` (bytes)
- `LOG_LEVEL` (default `INFO`)
- `PDF_IMAGE_MIN_WIDTH` (default `200`)
- `PDF_IMAGE_MIN_HEIGHT` (default `200`)
- `PDF_IMAGE_MAX_ASPECT_RATIO` (default `8.0`)

## Disk response contract

Example:

```json
{
	"task": "split",
	"response": {
		"type": "disk",
		"files": [
			{
				"path": "page_001.pdf",
				"label": "page_001.pdf",
				"type": "pdf",
				"extension": "pdf"
			}
		]
	}
}
```

Adapter (`elg_fs`) sends one `/tmp` callback per file using filename-only `tmp_path`.





