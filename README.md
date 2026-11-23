
# MD-pypdf_fs

An experimental MessyDesk wrapper for:
https://github.com/py-pdf/pypdf

The purpose of this service is to split a PDF into one-page PDF files.
It reads and writes directly under MessyDesk file storage (`_fs`).

## Task

- `split`: splits a PDF (file type `original-pdf`) into one-page PDF files `page_NNN.pdf`, named
  after the page number in the PDF. It is a normal cruncher task. Text and images of the pages
  come from MD-poppler, which only takes the pages (file type `pdf`).

Task params:

- `pages`: page range, e.g. `1-5,8,10-` (`N`, `N-M`, `N-` and `-M`, 1-based, comma separated).
  Empty means all pages. A bad range gives status 400; a range that matches no page gives 422,
  except in a set run (`set_process` in the message), where such a PDF gives no pages and no error.
- `delete_original`: handled by MessyDesk after the split (default on), not by this service.

Each page keeps the document's metadata (title, author, dates), since the original PDF is
usually removed after the split. Every file of the answer has its `page_number`. A page that
cannot be written is skipped; the answer lists it in `response.failed_pages` (`[{page, error}]`,
with `response.page_count` and `response.selected_pages`), which MessyDesk shows on the split
process. When no page can be written the job fails with status 422 and the reason.

Outputs are written to MessyDesk's `data/<db>/tmp/`, from where MessyDesk moves them into the
project (disk mode), or served from `/files` (HTTP mode).

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

Important:
- Without a usable `MD_PATH` the service starts in HTTP mode (see "Storage modes" below).
- Disk responses use filename-only `response.files[].path` values (no absolute path).

Path notes:

- Service resolves runtime root from `MD_PATH` and validates it contains `data/`.

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






## Storage modes

The service picks its mode at start-up:

- **Disk mode** when `MD_PATH` points at the MessyDesk root (the directory that contains `data/`). The service reads the input from `message.file.path`, writes its output to `MD_PATH/data/<db>/tmp/`, and `/config` reports the `elg_fs` adapter. In a container, mount MessyDesk's `data/` and set `MD_PATH` to the mount's parent directory, for example `-v /path/to/MessyDesk/data:/app/data -e MD_PATH=/app`.
- **HTTP mode** when `MD_PATH` is unset or has no `data/`. The input comes as the `content` upload, outputs are served from `/files`, and `/config` reports the `elg` adapter. `STORAGE_MODE=http` forces this mode.

A request that uploads `content` is always handled in HTTP mode. `SERVICE_ADAPTER` overrides the adapter that `/config` reports.

In HTTP mode the outputs are kept in `OUTPUT_FOLDER` (default `./output`) until they are downloaded once from `/files/<id>/<name>`; outputs nobody downloads are removed after `OUTPUT_MAX_AGE_SECONDS` (default one day).
