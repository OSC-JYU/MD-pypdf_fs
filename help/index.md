# MD-pyPDF_FS Service

The **MD-pypdf_fs** service is a PDF service backed by the Python open-source library **`pypdf`**. It provides lightweight, memory-efficient operations for slicing and extracting data from PDF documents page by page.

For advanced text extraction or native-resolution image parsing, see the **MD-Poppler Service** help file.

---

## Available Crunchers

### `split`
Splits a single multi-page PDF document into a set of individual, one-page PDF nodes.

* **Why this is important:** Large, hundred-megabyte PDF files can be incredibly resource-heavy and memory-hungry to process. Breaking a document into a flat set of single pages ensures you can run downstream analysis (like OCR or classification) efficiently without running into out-of-memory errors or system slowdowns.

### `extract_text`
Extracts the embedded, structural text layers from a PDF, generating exactly **one text file (`.txt`) per page**.

* > ⚠️ **Note:** Like other native PDF text-extraction utilities, this cruncher reads digital text strings embedded in the file structure. It does not perform Optical Character Recognition (OCR) on flat, scanned images.

### `extract_images`
Extracts inline, embedded image assets out of each individual page file.

---

## External Resources
For developers or advanced users wishing to dig deeper into the underlying processing engine's architecture and capabilities, consult the official documentation:
* [pypdf Documentation](https://pypdf.readthedocs.io/en/stable/)
