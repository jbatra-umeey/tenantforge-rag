import csv
import io
import multiprocessing
import re
import unicodedata
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from bs4 import BeautifulSoup
from docx import Document
from docx.table import Table as DocxTable
from pypdf import PdfReader

ALLOWED = {".pdf", ".docx", ".html", ".htm", ".csv", ".txt", ".md"}


@dataclass
class Block:
    text: str
    locator: str


def clean(text):
    text = unicodedata.normalize("NFKC", text).replace("\x00", "")
    return re.sub(r"\s+", " ", text).strip()


def parse(data, name, max_chars=1_000_000):
    ext = Path(name).suffix.lower()
    blocks = []
    total = 0

    def add(value, locator):
        nonlocal total
        value = clean(value)
        total += len(value)
        if total > max_chars:
            raise ValueError("Extracted text exceeds limit")
        if value:
            blocks.append(Block(value, locator[:150]))

    if ext == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise ValueError("PDF signature is invalid")
        pdf = PdfReader(io.BytesIO(data))
        if pdf.is_encrypted:
            raise ValueError("Encrypted PDFs are not supported")
        if len(pdf.pages) > 500:
            raise ValueError("PDF page limit exceeded")
        for i, page in enumerate(pdf.pages, 1):
            add(page.extract_text() or "", f"page {i}")
    elif ext == ".docx":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(x.file_size for x in z.infolist()) > 30_000_000 or len(z.infolist()) > 2000:
                raise ValueError("DOCX archive expansion limit exceeded")
            if "word/document.xml" not in z.namelist():
                raise ValueError("Invalid DOCX")
        doc = Document(io.BytesIO(data))
        heading = ""
        for i, element in enumerate(doc.iter_inner_content(), 1):
            if isinstance(element, DocxTable):
                for j, row in enumerate(element.rows, 1):
                    add(" | ".join(c.text for c in row.cells), f"table {i}, row {j}")
            else:
                if element.style and element.style.name.startswith("Heading"):
                    heading = clean(element.text)
                elif element.text.strip():
                    add(f"{heading}: {element.text}" if heading else element.text, f"paragraph {i}")
    elif ext in {".html", ".htm"}:
        soup = BeautifulSoup(data.decode("utf-8-sig"), "html.parser")
        for element in soup(["script", "style", "iframe", "noscript", "nav", "header", "footer"]):
            element.decompose()
        # Paragraph-level sections retain headings; no remote URLs are fetched.
        heading = "HTML"
        elements = soup.find_all(["h1", "h2", "h3", "p", "li", "tr", "pre"])
        for element in elements:
            if element.find_parent(["p", "li", "tr", "pre"]):
                continue
            if element.name.startswith("h"):
                heading = clean(element.get_text(" "))[:100]
                continue
            add(f"{heading}: {element.get_text(' ')}", heading)
        if not elements:
            add(soup.get_text(" "), "HTML body")
    elif ext == ".csv":
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
        if not reader.fieldnames or len(reader.fieldnames) > 200:
            raise ValueError("CSV requires a bounded header")
        for i, row in enumerate(reader, 2):
            add("; ".join(f"{k}: {v}" for k, v in row.items() if k is not None), f"row {i}")
    elif ext in {".txt", ".md"}:
        for i, paragraph in enumerate(re.split(r"\n\s*\n", data.decode("utf-8-sig")), 1):
            add(paragraph, f"section {i}")
    else:
        raise ValueError("Unsupported file type")
    if not blocks:
        raise ValueError("No extractable text; scanned PDFs require OCR before upload")
    return blocks


def chunk(blocks, target_chars=1200, overlap_chars=180, max_chunks=2000):
    """Sentence/paragraph-aware splitting. No claim of model-based topic segmentation."""
    out = []
    for block in blocks:
        pieces = re.split(r"(?<=[.!?])\s+", block.text)
        buffer = ""
        for sentence in pieces:
            # Hard splits bound pathological paragraphs or very long sentences.
            for part in [sentence[i:i + target_chars] for i in range(0, len(sentence), target_chars)]:
                if buffer and len(buffer) + len(part) + 1 > target_chars:
                    out.append(Block(buffer, block.locator))
                    tail = buffer[-overlap_chars:]
                    tail = tail.partition(" ")[2]
                    buffer = tail if len(tail) + len(part) + 1 <= target_chars else ""
                buffer = (buffer + " " + part).strip()
        if buffer:
            out.append(Block(buffer, block.locator))
        if len(out) > max_chunks:
            raise ValueError("Chunk limit exceeded")
    return out


def _parse_child(pipe, data, name, max_chars, max_chunks):
    try:
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
            resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
        except ImportError:
            pass
        result = chunk(parse(data, name, max_chars), max_chunks=max_chunks)
        pipe.send({"blocks": [asdict(x) for x in result]})
    except Exception:
        pipe.send({"error": "Document parsing failed or exceeded safety limits"})
    finally:
        pipe.close()


def parse_isolated(data, name, max_chars, max_chunks, timeout=30):
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_parse_child, args=(child, data, name, max_chars, max_chunks), daemon=True)
    process.start()
    child.close()
    try:
        if not parent.poll(timeout):
            raise ValueError("Parser timeout")
        result = parent.recv()
        if "error" in result:
            raise ValueError(result["error"])
        return [Block(**x) for x in result["blocks"]]
    except EOFError:
        raise ValueError("Parser exited before completion") from None
    finally:
        parent.close()
        process.join(timeout=1)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)

