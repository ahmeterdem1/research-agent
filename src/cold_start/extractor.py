from pathlib import Path
from dataclasses import dataclass
import chardet
from docx import Document as DocxDocument
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.pipeline_options import PdfPipelineOptions, AcceleratorOptions, AcceleratorDevice
from litellm import completion
import fitz
import base64
import os

from config import settings

@dataclass
class ExtractedContent:
    text: str
    headings: list[str]

def extract_content(file_path: str | Path) -> ExtractedContent | None:
    file_path = Path(file_path)
    ext = file_path.suffix.lower()

    if ext == ".pdf":
        mode = getattr(settings, "EXTRACTOR_MODE", "docling").lower()
        if mode == "llm":
            return extract_pdf_llm(file_path)
        else:
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
        # Explicitly configure GPU if DOCLING_DEVICE is set, else AUTO
        device_env = os.environ.get("DOCLING_DEVICE", "auto").lower()
        if device_env == "cuda":
            accel_device = AcceleratorDevice.CUDA
        elif device_env in ["mps", "mac"]:
            accel_device = AcceleratorDevice.MPS
        else:
            accel_device = AcceleratorDevice.AUTO
            
        print(f"  [Docling] Using accelerator device: {accel_device}")
        
        pipeline_options = PdfPipelineOptions(
            accelerator_options=AcceleratorOptions(
                device=accel_device,
                num_threads=8
            )
        )
        
        _converter_instance = DocumentConverter(
            format_options={
                "pdf": PdfFormatOption(pipeline_options=pipeline_options)
            }
        )
    return _converter_instance

def extract_pdf_docling(file_path: Path) -> ExtractedContent:
    converter = get_converter()
    result = converter.convert(file_path)
    doc = result.document
    
    text = doc.export_to_markdown()
    headings = [line.strip('# ').strip() for line in text.split('\n') if line.startswith('#')]
    
    return ExtractedContent(text=text, headings=headings)

def extract_pdf_llm(file_path: Path) -> ExtractedContent:
    """Extracts PDF text using a Multimodal LLM (Vision). Converts pages to images."""
    from config.prompts import get_prompts
    
    print(f"  [LLM OCR] Extracting {file_path.name} via LLM Vision...")
    text_parts = []
    
    prompts = get_prompts()
    prompt_template = prompts["extractor"]["llm_ocr_prompt"]
    prompt = prompt_template.format(num_images=1)
    
    try:
        # Open PDF and render pages to images
        doc = fitz.open(file_path)
        for page_num in range(doc.page_count):
            print(f"    -> Processing page {page_num + 1}/{doc.page_count}")
            page = doc.load_page(page_num)
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2)) # 2x resolution
            img_bytes = pix.tobytes("jpeg")
            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
            
            # Use the summarization model or a specific vision model from settings
            model = getattr(settings, "VISION_MODEL", settings.DEFAULT_SUMMARIZATION_MODEL)
            
            response = completion(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
                        ]
                    }
                ]
            )
            page_text = response.choices[0].message.content.strip()
            text_parts.append(page_text)
            
        doc.close()
        
        full_text = "\n\n".join(text_parts)
        headings = [line.strip('# ').strip() for line in full_text.split('\n') if line.startswith('#')]
        return ExtractedContent(text=full_text, headings=headings)
        
    except Exception as e:
        print(f"  [LLM OCR] Failed: {e}")
        return ExtractedContent(text=f"LLM Extraction failed: {e}", headings=[])

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

