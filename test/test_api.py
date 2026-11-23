import io
import json
import os
import sys
import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from typing import Optional

from fastapi import HTTPException
from pypdf import PdfWriter
from starlette.datastructures import UploadFile


class PdfSplitterApiTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.repo_root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(cls.repo_root))

        os.environ["MD_PATH"] = cls.tempdir.name
        # disk mode needs MD_PATH/data, otherwise the service falls back to HTTP mode
        (Path(cls.tempdir.name) / "data").mkdir()
        os.environ["CONTAINER"] = "false"

        if "api" in sys.modules:
            del sys.modules["api"]
        cls.api = importlib.import_module("api")

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def _create_pdf_in_md_path(self, rel_pdf_path: str, page_count: int = 2) -> Path:
        abs_pdf_path = Path(self.tempdir.name) / rel_pdf_path
        abs_pdf_path.parent.mkdir(parents=True, exist_ok=True)

        writer = PdfWriter()
        for _ in range(page_count):
            writer.add_blank_page(width=595, height=842)

        with open(abs_pdf_path, "wb") as handle:
            writer.write(handle)

        return abs_pdf_path

    def _create_broken_pdf_with_prefix(self, rel_pdf_path: str, page_count: int = 2) -> Path:
        valid = self._create_pdf_in_md_path(rel_pdf_path, page_count=page_count)
        original = valid.read_bytes()
        valid.write_bytes(b"-----BROKEN-PREFIX-----\n" + original)
        return valid

    def _build_message(self, rel_pdf_path: str, task_id: str = "split", label: Optional[str] = None, params: Optional[dict] = None) -> dict:
        file_label = label if label is not None else os.path.basename(rel_pdf_path)
        return {
            "service": {"id": "md-pdf-splitter_fs"},
            "task": {"id": task_id, "params": params or {}},
            "file": {
                "@rid": "#79:18",
                "project_rid": "#1:4",
                "path": rel_pdf_path,
                "label": file_label,
                "type": "original-pdf",
            },
            "process": {"@rid": "#106:13"},
            "userId": "#49:0",
        }

    async def _call_process(self, payload: dict):
        content = json.dumps(payload).encode("utf-8")
        upload = UploadFile(filename="request.json", file=io.BytesIO(content))
        return await self.api.process_files(upload, None)

    def _tmp_dir(self) -> Path:
        return Path(self.tempdir.name) / "data/dir_test/tmp"

    def _assert_outputs_in_tmp(self, result: dict, names):
        files = result["response"]["files"]
        self.assertEqual([f["label"] for f in files], names)
        for f in files:
            self.assertTrue((self._tmp_dir() / f["path"]).exists(), f["path"])

    def _assert_no_leftovers(self, source_pdf: Path):
        self.assertFalse((source_pdf.parent / "pages").exists())
        self.assertEqual([p.name for p in self._tmp_dir().iterdir() if p.name.startswith(".pypdf_")], [])

    async def test_split_success_creates_page_files(self):
        rel_pdf_path = "data/dir_test/projects/1_4/files/a/b/c/source/source.pdf"
        source = self._create_pdf_in_md_path(rel_pdf_path, page_count=3)

        result = await self._call_process(self._build_message(rel_pdf_path))

        self.assertEqual(result["response"]["page_count"], 3)
        self.assertEqual(result["response"]["failed_pages"], [])
        self.assertEqual(result["response"]["type"], "disk")
        self._assert_outputs_in_tmp(result, ["page_001.pdf", "page_002.pdf", "page_003.pdf"])
        self.assertEqual({f["type"] for f in result["response"]["files"]}, {"pdf"})
        self.assertEqual([f["page_number"] for f in result["response"]["files"]], [1, 2, 3])
        self._assert_no_leftovers(source)

    async def test_page_range_splits_only_the_selected_pages(self):
        rel_pdf_path = "data/dir_test/projects/1_4/files/a/b/c/source/ranged.pdf"
        source = self._create_pdf_in_md_path(rel_pdf_path, page_count=12)

        result = await self._call_process(self._build_message(rel_pdf_path, params={"pages": "2-3, 5,11-"}))

        # the file names and page numbers are those of the pages in the PDF
        names = ["page_002.pdf", "page_003.pdf", "page_005.pdf", "page_011.pdf", "page_012.pdf"]
        self._assert_outputs_in_tmp(result, names)
        self.assertEqual([f["page_number"] for f in result["response"]["files"]], [2, 3, 5, 11, 12])
        self.assertEqual(result["response"]["page_count"], 12)
        self.assertEqual(result["response"]["selected_pages"], 5)
        self._assert_no_leftovers(source)

    async def test_empty_page_range_splits_all_pages(self):
        rel_pdf_path = "data/dir_test/projects/1_4/files/a/b/c/source/allpages.pdf"
        self._create_pdf_in_md_path(rel_pdf_path, page_count=2)
        result = await self._call_process(self._build_message(rel_pdf_path, params={"pages": " "}))
        self.assertEqual(len(result["response"]["files"]), 2)

    async def test_invalid_page_range_is_rejected(self):
        rel_pdf_path = "data/dir_test/projects/1_4/files/a/b/c/source/badrange.pdf"
        self._create_pdf_in_md_path(rel_pdf_path, page_count=2)
        for bad in ("0", "3-1", "x", "1-2-3", "1,,2"):
            with self.assertRaises(HTTPException, msg=bad) as ctx:
                await self._call_process(self._build_message(rel_pdf_path, params={"pages": bad}))
            self.assertEqual(ctx.exception.status_code, 400, bad)

    async def test_page_range_outside_the_document_fails(self):
        rel_pdf_path = "data/dir_test/projects/1_4/files/a/b/c/source/outside.pdf"
        self._create_pdf_in_md_path(rel_pdf_path, page_count=2)
        with self.assertRaises(HTTPException) as ctx:
            await self._call_process(self._build_message(rel_pdf_path, params={"pages": "5-9"}))
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("matches none", ctx.exception.detail)

    async def test_set_run_pdf_shorter_than_the_range_gives_no_pages(self):
        # one short PDF in a set must not fail the job (it was retried and counted as failed)
        rel_pdf_path = "data/dir_test/projects/1_4/files/a/b/c/source/short_in_set.pdf"
        source = self._create_pdf_in_md_path(rel_pdf_path, page_count=2)
        message = self._build_message(rel_pdf_path, params={"pages": "5-"})
        message["set_process"] = "#55:21"

        result = await self._call_process(message)

        self.assertEqual(result["response"]["files"], [])
        self.assertEqual(result["response"]["page_count"], 2)
        self.assertEqual(result["response"]["selected_pages"], 0)
        self._assert_no_leftovers(source)

    async def test_broken_pdf_prefix_is_fixed_and_split(self):
        rel_pdf_path = "data/dir_test/projects/1_4/files/a/b/c/source/broken.pdf"
        source = self._create_broken_pdf_with_prefix(rel_pdf_path, page_count=2)

        result = await self._call_process(self._build_message(rel_pdf_path))

        self.assertEqual(result["response"]["page_count"], 2)
        self.assertEqual(len(result["response"]["files"]), 2)
        self._assert_no_leftovers(source)

    async def test_missing_file_returns_404(self):
        rel_pdf_path = "data/dir_test/projects/1_4/files/a/b/c/source/does_not_exist.pdf"

        with self.assertRaises(HTTPException) as ctx:
            await self._call_process(self._build_message(rel_pdf_path))

        self.assertEqual(ctx.exception.status_code, 404)

    async def test_http_mode_serves_outputs_from_files(self):
        writer = PdfWriter()
        for _ in range(2):
            writer.add_blank_page(width=595, height=842)
        pdf = io.BytesIO()
        writer.write(pdf)
        pdf.seek(0)
        message = self._build_message("upload.pdf")
        del message["file"]["path"]
        upload = UploadFile(filename="request.json", file=io.BytesIO(json.dumps(message).encode("utf-8")))

        with tempfile.TemporaryDirectory() as out, patch.object(self.api, "OUTPUT_FOLDER", Path(out).resolve()):
            result = await self.api.process_files(upload, UploadFile(filename="upload.pdf", file=pdf))
            uris = result["response"]["uri"]
            self.assertEqual(result["response"]["type"], "stored")
            self.assertEqual([u["uri"].rsplit("/", 1)[1] for u in uris], ["page_001.pdf", "page_002.pdf"])
            self.assertEqual([u["page_number"] for u in uris], [1, 2])
            output_id = uris[0]["uri"].split("/")[2]
            self.assertEqual(sorted(p.name for p in (Path(out) / output_id).iterdir()), ["page_001.pdf", "page_002.pdf"])

    async def test_rejects_path_traversal(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._call_process(self._build_message("../../etc/passwd"))

        self.assertEqual(ctx.exception.status_code, 400)

    async def test_rejects_absolute_path(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._call_process(self._build_message("/tmp/evil.pdf"))

        self.assertEqual(ctx.exception.status_code, 400)

    async def test_a_page_that_cannot_be_written_is_reported(self):
        rel_pdf_path = "data/dir_test/projects/1_4/files/a/b/c/source/partly.pdf"
        self._create_pdf_in_md_path(rel_pdf_path, page_count=3)
        real_write = self.api.PdfWriter.write
        calls = {"n": 0}

        def flaky_write(writer, stream):
            calls["n"] += 1
            if calls["n"] == 2:
                raise ValueError("broken content stream")
            return real_write(writer, stream)

        with patch.object(self.api.PdfWriter, "write", flaky_write):
            result = await self._call_process(self._build_message(rel_pdf_path))

        self.assertEqual(result["response"]["page_count"], 3)
        self.assertEqual(result["response"]["failed_pages"], [{"page": 2, "error": "broken content stream"}])
        self._assert_outputs_in_tmp(result, ["page_001.pdf", "page_003.pdf"])

    async def test_no_page_written_is_an_error_for_the_user(self):
        rel_pdf_path = "data/dir_test/projects/1_4/files/a/b/c/source/hopeless.pdf"
        self._create_pdf_in_md_path(rel_pdf_path, page_count=2)

        def failing_write(writer, stream):
            raise ValueError("broken content stream")

        with patch.object(self.api.PdfWriter, "write", failing_write), self.assertRaises(HTTPException) as ctx:
            await self._call_process(self._build_message(rel_pdf_path))

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("None of the 2 pages could be split", ctx.exception.detail)

    async def test_pages_keep_the_document_metadata(self):
        rel_pdf_path = "data/dir_test/projects/1_4/files/a/b/c/source/meta.pdf"
        abs_pdf_path = Path(self.tempdir.name) / rel_pdf_path
        abs_pdf_path.parent.mkdir(parents=True, exist_ok=True)
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        writer.add_metadata({"/Title": "Annual report 1921", "/Author": "Board"})
        with open(abs_pdf_path, "wb") as handle:
            writer.write(handle)

        result = await self._call_process(self._build_message(rel_pdf_path))

        page = self._tmp_dir() / result["response"]["files"][0]["path"]
        metadata = self.api.PdfReader(str(page)).metadata
        self.assertEqual(metadata.title, "Annual report 1921")
        self.assertEqual(metadata.author, "Board")

    async def test_only_split_is_a_task(self):
        rel_pdf_path = "data/dir_test/projects/1_4/files/a/b/c/source/task.pdf"
        self._create_pdf_in_md_path(rel_pdf_path, page_count=1)
        for task_id in ("extract_text", "extract_images"):
            with self.assertRaises(HTTPException) as ctx:
                await self._call_process(self._build_message(rel_pdf_path, task_id=task_id))
            self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
