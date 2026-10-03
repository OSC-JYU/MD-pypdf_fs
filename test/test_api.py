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
from types import SimpleNamespace

from fastapi import HTTPException
from pypdf import PdfWriter
from PIL import Image
from starlette.datastructures import UploadFile


class PdfSplitterApiTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.repo_root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(cls.repo_root))

        os.environ["MD_PATH"] = cls.tempdir.name
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

    def _build_message(self, rel_pdf_path: str, task_id: str = "split", label: Optional[str] = None) -> dict:
        file_label = label if label is not None else os.path.basename(rel_pdf_path)
        return {
            "service": {"id": "md-pdf-splitter_fs"},
            "task": {"id": task_id, "params": {}},
            "file": {
                "@rid": "#79:18",
                "project_rid": "#1:4",
                "path": rel_pdf_path,
                "label": file_label,
                "type": "pdf",
            },
            "process": {"@rid": "#106:13"},
            "userId": "#49:0",
        }

    async def _call_process(self, payload: dict):
        content = json.dumps(payload).encode("utf-8")
        upload = UploadFile(filename="request.json", file=io.BytesIO(content))
        return await self.api.process_files(upload)

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

    def _png_bytes(self, width: int, height: int) -> bytes:
        buffer = io.BytesIO()
        img = Image.new("RGB", (width, height), color=(10, 20, 30))
        img.save(buffer, format="PNG")
        return buffer.getvalue()

    async def test_split_success_creates_page_files(self):
        rel_pdf_path = "data/dir_test/projects/1_4/files/a/b/c/source/source.pdf"
        source = self._create_pdf_in_md_path(rel_pdf_path, page_count=3)

        result = await self._call_process(self._build_message(rel_pdf_path))

        self.assertEqual(result["page_count"], 3)
        self.assertEqual(result["successful_pages"], 3)
        self.assertEqual(result["response"]["type"], "disk")
        self._assert_outputs_in_tmp(result, ["page_001.pdf", "page_002.pdf", "page_003.pdf"])
        self.assertEqual({f["type"] for f in result["response"]["files"]}, {"pdf"})
        self._assert_no_leftovers(source)

    async def test_broken_pdf_prefix_is_fixed_and_split(self):
        rel_pdf_path = "data/dir_test/projects/1_4/files/a/b/c/source/broken.pdf"
        source = self._create_broken_pdf_with_prefix(rel_pdf_path, page_count=2)

        result = await self._call_process(self._build_message(rel_pdf_path))

        self.assertEqual(result["page_count"], 2)
        self.assertEqual(result["successful_pages"], 2)
        self.assertEqual(len(result["response"]["files"]), 2)
        self._assert_no_leftovers(source)

    async def test_missing_file_returns_404(self):
        rel_pdf_path = "data/dir_test/projects/1_4/files/a/b/c/source/does_not_exist.pdf"

        with self.assertRaises(HTTPException) as ctx:
            await self._call_process(self._build_message(rel_pdf_path))

        self.assertEqual(ctx.exception.status_code, 404)

    async def test_rejects_path_traversal(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._call_process(self._build_message("../../etc/passwd"))

        self.assertEqual(ctx.exception.status_code, 400)

    async def test_rejects_absolute_path(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._call_process(self._build_message("/tmp/evil.pdf"))

        self.assertEqual(ctx.exception.status_code, 400)

    async def test_extract_text_creates_page_text_files(self):
        rel_pdf_path = "data/dir_test/projects/1_4/files/a/b/c/source/textsource.pdf"
        source = self._create_pdf_in_md_path(rel_pdf_path, page_count=2)

        result = await self._call_process(self._build_message(rel_pdf_path, task_id="extract_text"))

        self.assertEqual(result["page_count"], 2)
        self.assertEqual(result["successful_pages"], 2)
        self._assert_outputs_in_tmp(result, ["textsource_page_1.pdf.txt", "textsource_page_2.pdf.txt"])
        for f in result["response"]["files"]:
            self.assertEqual((f["type"], f["extension"]), ("text", "txt"))
        self._assert_no_leftovers(source)

    async def test_extract_text_preserves_split_page_number_in_filename(self):
        rel_pdf_path = "data/dir_test/projects/1_4/files/a/b/c/source/page_12.pdf"
        self._create_pdf_in_md_path(rel_pdf_path, page_count=1)

        result = await self._call_process(self._build_message(rel_pdf_path, task_id="extract_text"))

        self.assertEqual(result["page_count"], 1)
        self.assertEqual(result["successful_pages"], 1)

        self.assertEqual([f["label"] for f in result["response"]["files"]], ["page_12.pdf.txt"])

    async def test_extract_text_uses_file_label_not_storage_basename(self):
        rel_pdf_path = "data/dir_test/projects/1_4/files/a/b/c/source/019d24a20c1777a9995d1b4d8a04a437.pdf"
        self._create_pdf_in_md_path(rel_pdf_path, page_count=1)

        result = await self._call_process(
            self._build_message(rel_pdf_path, task_id="extract_text", label="page_12.pdf")
        )

        self.assertEqual(result["page_count"], 1)
        self.assertEqual(result["successful_pages"], 1)

        self.assertEqual([f["label"] for f in result["response"]["files"]], ["page_12.pdf.txt"])

    async def test_extract_images_set_processing_callbacks_use_image_metadata(self):
        rel_pdf_path = "data/dir_test/projects/1_4/files/a/b/c/source/queued_images.pdf"
        abs_pdf_path = self._create_pdf_in_md_path(rel_pdf_path, page_count=1)

        pages_dir = Path(tempfile.mkdtemp(dir=self.tempdir.name))
        image1 = pages_dir / "queued_images_page_1_image_1.png"
        image2 = pages_dir / "queued_images_page_1_image_2.jpg"
        image1.write_bytes(b"\x89PNG\r\n\x1a\n")
        image2.write_bytes(b"\xff\xd8\xff\xe0")

        payload = self._build_message(rel_pdf_path, task_id="extract_images", label="queued_images.pdf")

        fake_result = {
            "page_count": 1,
            "successful_pages": 2,
            "image_paths": [str(image1), str(image2)],
        }

        with patch("api.extract_images_from_pdf", return_value=fake_result):
            result = await self._call_process(payload)

        self.assertEqual(result["successful_pages"], 2)
        call1, call2 = result["response"]["files"]
        self.assertEqual(call1["type"], "image")
        self.assertEqual(call2["type"], "image")
        self.assertEqual(call1["extension"], "png")
        self.assertEqual(call2["extension"], "jpg")

    def test_extract_images_skips_small_and_extreme_aspect_ratio_by_default(self):
        rel_pdf_path = "data/dir_test/projects/1_4/files/a/b/c/source/filtering_source.pdf"
        abs_pdf_path = self._create_pdf_in_md_path(rel_pdf_path, page_count=1)

        fake_images = [
            SimpleNamespace(name="small.png", data=self._png_bytes(150, 300)),
            SimpleNamespace(name="thin.png", data=self._png_bytes(200, 2000)),
            SimpleNamespace(name="ok.png", data=self._png_bytes(400, 300)),
        ]
        fake_reader = SimpleNamespace(pages=[SimpleNamespace(images=fake_images)])

        with patch("api.PdfReader", return_value=fake_reader):
            with tempfile.TemporaryDirectory() as work_dir:
                result = self.api.extract_images_from_pdf(str(abs_pdf_path), work_dir, source_label="filtering_source.pdf")

        self.assertEqual(result["page_count"], 1)
        self.assertEqual(result["successful_pages"], 1)
        self.assertEqual(len(result["image_paths"]), 1)
        self.assertTrue(result["image_paths"][0].endswith("filtering_source_page_1_image_3.png"))

    async def test_extract_images_uses_task_filter_params(self):
        rel_pdf_path = "data/dir_test/projects/1_4/files/a/b/c/source/filtering_params.pdf"
        self._create_pdf_in_md_path(rel_pdf_path, page_count=1)

        payload = self._build_message(rel_pdf_path, task_id="extract_images", label="filtering_params.pdf")
        payload["task"]["params"] = {
            "min_width": 321,
            "min_height": 222,
            "max_aspect_ratio": 3.5,
        }

        fake_result = {"page_count": 1, "successful_pages": 0, "image_paths": []}
        with patch("api.extract_images_from_pdf", return_value=fake_result) as mocked_extract:
            result = await self._call_process(payload)

        self.assertEqual(result["page_count"], 1)
        self.assertEqual(result["successful_pages"], 0)
        mocked_extract.assert_called_once()
        _, kwargs = mocked_extract.call_args
        self.assertEqual(kwargs["min_width"], 321)
        self.assertEqual(kwargs["min_height"], 222)
        self.assertEqual(kwargs["max_aspect_ratio"], 3.5)


if __name__ == "__main__":
    unittest.main()
