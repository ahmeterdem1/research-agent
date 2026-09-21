from pathlib import Path
from dataclasses import dataclass
import chardet
from docx import Document as DocxDocument
from docling.document_converter import DocumentConverter

@dataclass
class ExtractedContent:
    text: str
    headings: list[str]

def extract_content(file_path: str | Path) -> ExtractedContent | None:
    file_path = Path(file_path)
    ext = file_path.suffix.lower()

    if ext == ".pdf":
        return extract_pdf_docling(file_path)
    elif ext in {".txt", ".md"}:
        return extract_text(file_path)
    elif ext == ".docx":
        return extract_docx(file_path)
    return None

_converter_instance = None

def get_converter() -> DocumentConverter:
    global _converter_instance
    if _converter_instance is None:
        print("  [Docling] Loading models into memory (this only happens once)...")
        _converter_instance = DocumentConverter()
    return _converter_instance

def extract_pdf_docling(file_path: Path) -> ExtractedContent:
    converter = get_converter()
    result = converter.convert(file_path)
    doc = result.document
    
    text = doc.export_to_markdown()
    # Simple extraction of headings from markdown for our purposes
    headings = [line.strip('# ').strip() for line in text.split('\n') if line.startswith('#')]
    
    return ExtractedContent(text=text, headings=headings)

def extract_text(file_path: Path) -> ExtractedContent:
    raw_data = file_path.read_bytes()
    encoding = chardet.detect(raw_data)['encoding'] or 'utf-8'
    text = raw_data.decode(encoding, errors='replace')
    headings = [line.strip('# ').strip() for line in text.split('\n') if line.startswith('#')]
    return ExtractedContent(text=text, headings=headings)

def extract_docx(file_path: Path) -> ExtractedContent:
    doc = DocxDocument(file_path)
    text = "\n".join(para.text for para in doc.paragraphs)
    headings = [para.text for para in doc.paragraphs if para.style.name.startswith('Heading')]
    return ExtractedContent(text=text, headings=headings)

