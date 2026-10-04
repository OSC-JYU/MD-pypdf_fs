# PyPDF (PDF splitter)

Splits every PDF you import into **one PDF per page**. You don't run it yourself: MessyDesk does
it when a PDF is uploaded or unpacked from a ZIP, and the pages appear as a set named after the
PDF ("… — PDF pages"). The original PDF is then removed, so each page is its own file that you
can run crunchers on, view and tag.

It uses [pypdf](https://pypdf.readthedocs.io/).

## Why pages

Many crunchers handle one page at a time, and large PDFs are slow and memory-hungry to process as
a whole. With pages you can also pick just the pages you need, run a cruncher on a set of pages,
and see results page by page.

Each page keeps the document's information (title, author, creation date), which **Poppler →
PDF info** shows.

## What to use next

| To get | Use |
|---|---|
| The text of a page (digital PDFs) | **Poppler → Extract text from PDF** |
| A page as an image (for OCR, viewing) | **Poppler → Render PDF as images** |
| The images embedded in a page | **Poppler → Extract images from PDF** |
| Title, author, dates, page size | **Poppler → PDF info** |
| Text of scanned pages | render them as images, then **Tesseract** or another OCR cruncher |

## When something goes wrong

- If some pages cannot be split (a damaged page), the others are imported and the import process
  says which pages are missing and why.
- If no page can be split, the import fails and the process shows the reason. Try repairing the
  PDF (e.g. by printing it to a new PDF) and import it again.
- PDFs with junk before the start of the file (common in downloads) are repaired automatically.
