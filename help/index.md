# PyPDF (PDF splitter)

Splits a PDF into **one PDF per page**. It is the cruncher for an uploaded PDF: choose **Split PDF
to pages** on the PDF and the pages appear as a set named "PDF pages". Each page is its own file
that you can run crunchers on, view and tag.

It uses [pypdf](https://pypdf.readthedocs.io/).

## Options

| Option | Meaning |
|---|---|
| **Pages** | The pages to split, for example `1-5,8,10-`. Leave empty to split all pages. A page keeps its number in the PDF (`page_008.pdf`), so you can use the same numbers with **Poppler** later. |
| **Delete the original PDF after splitting** | On by default: the PDF is removed to save space and the node stays on the desk as a record. Turn it off to keep the file. |

> ⚠️ If you split only some pages and delete the original, the other pages are lost for good.
> Turn the deletion off when you may need them.

Range examples: `3` (page 3), `2-4` (pages 2 to 4), `10-` (page 10 to the end), `-5` (the first
five pages), `1,3,7-9`.

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

- If some pages cannot be split (a damaged page), the others are split and the process says which
  pages are missing and why.
- If no page can be split, the job fails and shows the reason. Try repairing the PDF (e.g. by
  printing it to a new PDF) and split it again.
- If the range does not match any page of a single PDF, the job fails; check the number of pages.
  When splitting a set, a PDF with no pages in the range gives no pages and is kept.
- PDFs with junk before the start of the file (common in downloads) are repaired automatically.
