import os
import tempfile
import unittest

from app.documents.extract import chunk_document_text, extract_text
from app.documents.service import DocumentLibrary, looks_like_document_request
from app.memory.crypto import PrivateBox
from app.memory.embedder import HashEmbedder
from app.memory.store import MemoryStore
from app.db.sqlite import SqliteDatabase


def make_pdf(text: str) -> bytes:
    """Build a tiny real PDF with reportlab (available in this environment and CI)."""
    import io

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for line in text.split("\n"):
        c.drawString(40, y, line)
        y -= 20
    c.save()
    return buf.getvalue()


def make_docx(text: str) -> bytes:
    import io

    from docx import Document

    doc = Document()
    for line in text.split("\n"):
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


class TestExtraction(unittest.TestCase):
    def test_pdf(self):
        data = make_pdf("BMI Manufacturing Agreement\nMinimum order quantities apply.")
        text = extract_text(data, "bmi-contract.pdf")
        self.assertIn("BMI Manufacturing Agreement", text)

    def test_docx(self):
        data = make_docx("Villa Statement of Account\nPaid to date: AED 3,100,000")
        text = extract_text(data, "villa-soa.docx")
        self.assertIn("AED 3,100,000", text)

    def test_plain_text(self):
        self.assertEqual(extract_text(b"hello world", "notes.txt"), "hello world")

    def test_unknown_binary_returns_empty(self):
        self.assertEqual(extract_text(b"\x00\x01\x02", "photo.jpg", "image/jpeg"), "")

    def test_chunking_covers_all_text(self):
        text = "\n\n".join(f"Paragraph {i} " + ("content " * 40) for i in range(10))
        chunks = chunk_document_text(text, target=500, overlap=50)
        self.assertTrue(all(len(c) <= 700 for c in chunks))
        for i in range(10):
            self.assertTrue(any(f"Paragraph {i}" in c for c in chunks))

    def test_request_detector(self):
        self.assertTrue(looks_like_document_request("show me the BMI contract"))
        self.assertTrue(looks_like_document_request("can you send me the villa statement of account file"))
        self.assertTrue(looks_like_document_request("where's the Sobha SOA document?"))
        self.assertFalse(looks_like_document_request("what should I do first today?"))
        self.assertFalse(looks_like_document_request("log my run"))


class TestLibrary(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.memory = MemoryStore(self.db, HashEmbedder(), PrivateBox(""))
        self.library = DocumentLibrary(self.db, self.memory, object_store=None)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_ingest_find_fetch_roundtrip(self):
        pdf = make_pdf("BMI Manufacturing Agreement between Prodermis and BMI.\nDermal filler MOQ terms.")
        summary = await self.library.ingest(pdf, "bmi-contract.pdf", mime="application/pdf")
        self.assertGreater(summary["chars"], 0)
        self.assertGreater(summary["chunks"], 0)

        matches = await self.library.find("show me the BMI contract")
        self.assertEqual(matches[0]["filename"], "bmi-contract.pdf")

        data = await self.library.fetch_bytes(matches[0])
        self.assertEqual(data, pdf)

    async def test_document_content_recallable_in_conversation(self):
        await self.library.ingest(
            make_docx("Sobha villa statement: next demand AED 1.2M due January 2027"),
            "villa-soa.docx",
        )
        hits = await self.memory.search("when is the next villa demand due?")
        self.assertTrue(any("1.2M" in h["content"] for h in hits))
        self.assertTrue(all("villa-soa.docx" in h["content"] for h in hits[:1]))

    async def test_filename_fallback_when_semantics_miss(self):
        await self.library.ingest(b"\x00\x01", "scan-receipt.jpg", mime="image/jpeg")
        matches = await self.library.find("send me the receipt file")
        self.assertEqual(matches[0]["filename"], "scan-receipt.jpg")

    async def test_unreadable_file_still_stored_and_returnable(self):
        summary = await self.library.ingest(b"\x00\x01\x02", "photo.jpg", mime="image/jpeg")
        self.assertEqual(summary["chars"], 0)
        row = await self.db.fetch_one("SELECT content FROM documents WHERE id = ?", (summary["id"],))
        self.assertEqual(bytes(row["content"]), b"\x00\x01\x02")


if __name__ == "__main__":
    unittest.main()
