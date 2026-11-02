from pathlib import Path
from dataclasses import dataclass
import chardet
from docx import Document as DocxDocument
from litellm import completion
from tenacity import retry, stop_after_attempt, wait_exponential
import fitz
import base64
import os

from config import settings
from config.settings import get_logger

logger = get_logger(__name__)

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

def get_converter():
    global _converter_instance
    if _converter_instance is None:
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.pipeline_options import PdfPipelineOptions, AcceleratorOptions, AcceleratorDevice

        logger.info("[Docling] Loading models into memory (this only happens once)...")
        # Explicitly configure GPU if DOCLING_DEVICE is set, else AUTO
        device_env = os.environ.get("DOCLING_DEVICE", "auto").lower()
        if device_env == "cuda":
            accel_device = AcceleratorDevice.CUDA
        elif device_env in ["mps", "mac"]:
            accel_device = AcceleratorDevice.MPS
        else:
            accel_device = AcceleratorDevice.AUTO

        logger.info("[Docling] Using accelerator device: %s", accel_device)

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

import concurrent.futures
import threading

_ocr_semaphore: threading.BoundedSemaphore | None = None
_ocr_semaphore_size: int | None = None


def _get_ocr_semaphore() -> tuple[threading.BoundedSemaphore, int]:
    global _ocr_semaphore, _ocr_semaphore_size
    max_workers = max(1, int(getattr(settings, "MAX_CONCURRENT_REQUESTS", 5)))
    if _ocr_semaphore is None or _ocr_semaphore_size != max_workers:
        _ocr_semaphore = threading.BoundedSemaphore(max_workers)
        _ocr_semaphore_size = max_workers
    return _ocr_semaphore, max_workers


@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
def _ocr_single_page(page_num: int, total_pages: int, img_b64: str, prompt: str, model: str) -> str:
    sem, _ = _get_ocr_semaphore()
    with sem:
        logger.info("Processing page %d/%d", page_num + 1, total_pages)
        response = completion(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                    ],
                }
            ],
        )
        return response.choices[0].message.content.strip()


def extract_pdf_llm(file_path: Path) -> ExtractedContent:
    """
    Extracts PDF text using a Multimodal LLM (Vision) in parallel across pages,
    bounded by a global semaphore (`MAX_CONCURRENT_REQUESTS`).
    """
    from config.prompts import get_prompts

    _, max_workers = _get_ocr_semaphore()
    logger.info("[LLM OCR] Extracting %s via LLM Vision (concurrency=%d)...", file_path.name, max_workers)

    prompts = get_prompts()
    prompt_template = prompts["extractor"]["llm_ocr_prompt"]
    prompt = prompt_template.replace("{num_images}", "1")
    model = getattr(settings, "VISION_MODEL", settings.DEFAULT_SUMMARIZATION_MODEL)

    # Render pages to base64 JPEGs sequentially on the main thread (PyMuPDF is not thread-safe)
    page_images: list[tuple[int, str]] = []
    with fitz.open(file_path) as doc:
        total_pages = doc.page_count
        for page_num in range(total_pages):
            page = doc.load_page(page_num)
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # 2x resolution
            img_bytes = pix.tobytes("jpeg")
            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
            page_images.append((page_num, img_b64))

    text_parts: list[str] = [""] * len(page_images)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {
            executor.submit(_ocr_single_page, p_num, total_pages, b64, prompt, model): p_num
            for p_num, b64 in page_images
        }
        for future in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[future]
            text_parts[idx] = future.result()

    full_text = "\n\n".join(text_parts)
    headings = [line.strip("# ").strip() for line in full_text.split("\n") if line.startswith("#")]
    return ExtractedContent(text=full_text, headings=headings)

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

