"""Opt-in ~1GiB transport/processing exercise; requires ~5GiB free disk.

Run from the repository: python -m tests.manual_large_file
No network requests leave localhost. No Gemini API calls are made.
"""

import json
import resource
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import fitz
import requests

from pdf_processor import CHUNK_BYTES, MAX_UPLOAD_BYTES, bookmark_chapters
from transfer_server import TransferService


def make_one_gib_pdf(path: Path) -> None:
    """1023 independent 1MiB grayscale images plus PDF objects and padding."""
    pages = 1023
    outline_id = 3 + 3 * pages
    offsets = [0]
    pixels = bytes(range(256)) * 4096
    with path.open("wb") as stream:
        stream.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        def obj(body: bytes):
            number = len(offsets)
            offsets.append(stream.tell())
            stream.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
        obj(f"<< /Type /Catalog /Pages 2 0 R /Outlines {outline_id} 0 R >>".encode())
        kids = " ".join(f"{3 + 3 * i} 0 R" for i in range(pages))
        obj(f"<< /Type /Pages /Count {pages} /Kids [{kids}] >>".encode())
        for i in range(pages):
            page = 3 + 3 * i
            obj((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 256 256] /Contents {page + 1} 0 R "
                 f"/Resources << /XObject << /Im0 {page + 2} 0 R >> >> >>").encode())
            content = b"q 256 0 0 256 0 0 cm /Im0 Do Q\n"
            obj(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"endstream")
            obj((f"<< /Type /XObject /Subtype /Image /Width 1024 /Height 1024 "
                 f"/ColorSpace /DeviceGray /BitsPerComponent 8 /Length {len(pixels)} >>\nstream\n").encode()
                + pixels + b"\nendstream")
        obj(f"<< /Type /Outlines /Count 3 /First {outline_id + 1} 0 R /Last {outline_id + 3} 0 R >>".encode())
        for index, first_page in enumerate([0, 341, 682]):
            siblings = (f" /Prev {outline_id + index} 0 R" if index else "")
            siblings += (f" /Next {outline_id + index + 2} 0 R" if index < 2 else "")
            obj((f"<< /Title (Section {index + 1}) /Parent {outline_id} 0 R "
                 f"/Dest [{3 + first_page * 3} 0 R /XYZ null null null]{siblings} >>").encode())
        padding_id = len(offsets)
        offsets.append(stream.tell())
        prefix = lambda n: f"{padding_id} 0 obj\n<< /Length {n:010d} >>\nstream\n".encode()
        end_object = b"\nendstream\nendobj\n"
        xref = f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
        xref += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
        def ending(n):
            startxref = offsets[-1] + len(prefix(n)) + n + len(end_object)
            return end_object + xref + (f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
                                       f"startxref\n{startxref}\n%%EOF\n").encode()
        padding = 0
        for _ in range(3):
            padding = MAX_UPLOAD_BYTES - offsets[-1] - len(prefix(padding)) - len(ending(padding))
        assert padding > 0
        stream.write(prefix(padding))
        remaining = padding
        while remaining:
            block = pixels[:min(len(pixels), remaining)]
            stream.write(block)
            remaining -= len(block)
        stream.write(ending(padding))
    assert path.stat().st_size == MAX_UPLOAD_BYTES


def main():
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="pdf-large-verification-") as temp:
        root = Path(temp)
        source = root / "one-gib.pdf"
        make_one_gib_pdf(source)
        service = TransferService(root / "spool", port=0)
        client = requests.Session()
        client.trust_env = False
        try:
            session = service.store.create()
            base = f"http://127.0.0.1:{service.port}/{session.token}/"
            response = client.post(base + "start", json={"name": source.name, "size": source.stat().st_size}, timeout=30)
            response.raise_for_status()
            upload_id = response.json()["id"]
            offset = 0
            with source.open("rb") as stream:
                while block := stream.read(CHUNK_BYTES):
                    response = client.post(base + "chunk", params={"id": upload_id, "offset": offset}, data=block, timeout=30)
                    response.raise_for_status()
                    offset += len(block)
            client.post(base + "finish", json={"id": upload_id}, timeout=30).raise_for_status()
            print(json.dumps({"uploaded_bytes": offset, "pages": session.info.page_count}), flush=True)
            assert session.info.page_count == 1023
            directory = session.workspace.path
            service.store.process(session, bookmark_chapters(session.info),
                                  progress=lambda done, total: print(f"chapter {done}/{total}", flush=True))
            assert not (directory / "source.pdf").exists()
            assert not (directory / "chapter.pdf").exists()
            archive_size = (directory / "chapters.zip").stat().st_size
            download = root / "download.zip"
            with client.get(base + "download", stream=True, timeout=30) as response:
                response.raise_for_status()
                with download.open("wb") as stream:
                    for block in response.iter_content(CHUNK_BYTES):
                        stream.write(block)
            assert download.stat().st_size == archive_size
            deadline = time.monotonic() + 5
            while directory.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not directory.exists()
            counts = []
            with zipfile.ZipFile(download) as archive:
                assert archive.testzip() is None
                for name in archive.namelist():
                    extracted = Path(archive.extract(name, root / "verify"))
                    with fitz.open(extracted) as doc:
                        counts.append(doc.page_count)
                        assert doc[0].get_images()[0][2:4] == (1024, 1024)
                    extracted.unlink()
            assert counts == [341, 341, 341]
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            peak_mib = peak / (1024 * 1024 if sys.platform == "darwin" else 1024)
            print(json.dumps({"input_bytes": MAX_UPLOAD_BYTES, "zip_bytes": archive_size,
                              "chapter_pages": counts, "peak_process_rss_mib": round(peak_mib, 1),
                              "seconds": round(time.monotonic() - start, 1), "cleanup": "passed"}), flush=True)
        finally:
            client.close()
            service.close()


if __name__ == "__main__":
    main()
