from pathlib import Path
from collections import Counter
from dataclasses import dataclass
import re
import chardet
from docx import Document as DocxDocument
from litellm import completion
import litellm.exceptions as litellm_exc
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential
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
    if hasattr(settings, "reload_env_settings"):
        settings.reload_env_settings()
    file_path = Path(file_path)
    ext = file_path.suffix.lower()

    if ext == ".pdf":
        mode = getattr(settings, "EXTRACTOR_MODE", "pymupdf").lower().strip()
        if mode == "llm":
            return extract_pdf_llm(file_path)
        elif mode == "docling":
            return extract_pdf_docling(file_path)
        else:
            # Default: 'pymupdf' (also 'fast' / 'auto') — fast native structured PDF-to-Markdown
            return extract_pdf_pymupdf(file_path)
    elif ext in {".txt", ".md"}:
        return extract_text(file_path)
    elif ext == ".docx":
        return extract_docx(file_path)
    return None


_SECTION_HEADER_RE = re.compile(
    r"^(?:"
    r"abstract|introduction|background|related\s+work|preliminaries|method|methods|methodology|"
    r"approach|model|architecture|experiments|experimental\s+setup|results|discussion|analysis|"
    r"ablation\s+study|limitations|broader\s+impact|conclusion|conclusions|acknowledgements|"
    r"acknowledgments|references|bibliography|appendix|supplementary\s+material|"
    r"(?:[1-9]\d*(?:\.[1-9]\d*)*|[IVX]+)\.?\s+[A-Z][A-Za-z0-9\s,\-:()/]{2,85}"
    r")$",
    re.IGNORECASE,
)
_SUBSECTION_NUMBER_RE = re.compile(r"^[1-9]\d*\.[1-9]\d*(?:\.\d+)*\.?\s+[A-Z]")
_LIST_ITEM_RE = re.compile(r"^(?:[•◦▪\-*]|\(\d+\)|\d+\.|\[[0-9]+\])\s+")
_RUNNING_BANNER_RE = re.compile(
    r"^(?:published\s+as\s+a\s+conference\s+paper|preprint\.\s+under\s+review|"
    r"proceedings\s+of\s+the\s+\d+|arxiv:\d{4}\.\d{4,5})",
    re.IGNORECASE,
)


def _is_span_bold(span: dict) -> bool:
    flags = int(span.get("flags", 0))
    if flags & 16:  # PyMuPDF bit 4 = bold
        return True
    font_name = (span.get("font") or "").lower()
    return any(b in font_name for b in ("bold", "medi", "demi", "cmbx", " NimbusRomNo9L-Medi".lower()))


def extract_pdf_pymupdf(file_path: Path, allow_ocr_fallback: bool = True) -> ExtractedContent:
    """
    Fast, zero-cost native structured PDF-to-Markdown converter powered by PyMuPDF (`fitz`).

    Features:
    - Layout-aware 2-column reading order (`page.get_text("dict", sort=True)`).
    - Font-size + bold + academic section-numbering heuristics to reconstruct `#`, `##`, and `###` headings.
    - Geometric table extraction (`page.find_tables()` -> `tab.to_markdown()`).
    - Automatic de-hyphenation across line wraps and paragraph joining.
    - Running header/footer & standalone page-number stripping.
    - Automatic fallback to Docling if the PDF is a scanned image with no embedded text layer.
    """
    from src.mcp_servers.progress import report_mcp_progress

    size_counter: Counter[float] = Counter()
    pages_dict: list[dict] = []
    page_tables: list[list[tuple[tuple[float, float, float, float], str]]] = []

    with fitz.open(file_path) as doc:
        total_pages = doc.page_count
        report_mcp_progress(
            0,
            max(1, total_pages),
            f"[PyMuPDF] Extracting structured Markdown from '{file_path.name}' ({total_pages} pages, $0 cost)...",
        )

        for page_idx in range(total_pages):
            page = doc.load_page(page_idx)
            p_dict = page.get_text("dict", sort=True)
            pages_dict.append(p_dict)

            # Collect font-size distribution weighted by character count
            for block in p_dict.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        txt = (span.get("text") or "").strip()
                        if txt:
                            sz = round(float(span.get("size", 10.0)), 1)
                            size_counter[sz] += len(txt)

            # Extract any geometric tables on this page
            tabs_for_page: list[tuple[tuple[float, float, float, float], str]] = []
            try:
                finder = page.find_tables()
                for tab in getattr(finder, "tables", []):
                    if getattr(tab, "row_count", 0) >= 2 and getattr(tab, "col_count", 0) >= 2:
                        md_tab = tab.to_markdown().strip()
                        if md_tab:
                            tabs_for_page.append((tuple(tab.bbox), md_tab))
            except Exception:
                pass
            page_tables.append(tabs_for_page)

    body_size = size_counter.most_common(1)[0][0] if size_counter else 10.0
    md_blocks: list[str] = []
    headings: list[str] = []
    total_chars = 0

    def _in_table_bbox(bbox: tuple[float, float, float, float], table_bboxes: list[tuple[float, float, float, float]]) -> bool:
        cx = (bbox[0] + bbox[2]) / 2.0
        cy = (bbox[1] + bbox[3]) / 2.0
        for tx0, ty0, tx1, ty1 in table_bboxes:
            if (tx0 - 4 <= cx <= tx1 + 4) and (ty0 - 4 <= cy <= ty1 + 4):
                return True
        return False

    for page_idx, p_dict in enumerate(pages_dict):
        page_h = float(p_dict.get("height") or 792.0)
        tabs = page_tables[page_idx]
        tab_bboxes = [b for b, _ in tabs]
        emitted_tabs: set[int] = set()

        for block in p_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            b_bbox = tuple(block.get("bbox", (0.0, 0.0, 0.0, 0.0)))

            # Emit any table that appears vertically above or at this block
            for t_idx, (t_bbox, t_md) in enumerate(tabs):
                if t_idx not in emitted_tabs and t_bbox[1] <= b_bbox[1] + 8:
                    md_blocks.append(t_md)
                    emitted_tabs.add(t_idx)

            if _in_table_bbox(b_bbox, tab_bboxes):
                continue

            # Reconstruct lines in this block
            block_lines: list[tuple[str, float, bool]] = []
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                line_text = "".join(s.get("text", "") for s in spans).strip()
                line_text = re.sub(r"\s+", " ", line_text)
                if not line_text:
                    continue
                max_sz = max(round(float(s.get("size", body_size)), 1) for s in spans if (s.get("text") or "").strip())
                is_bold = any(_is_span_bold(s) for s in spans if (s.get("text") or "").strip())
                block_lines.append((line_text, max_sz, is_bold))

            if not block_lines:
                continue

            # Strip running headers/footers and standalone page numbers at top/bottom margins
            if len(block_lines) == 1:
                only_txt = block_lines[0][0]
                if (b_bbox[1] < page_h * 0.06 or b_bbox[3] > page_h * 0.92) and (
                    only_txt.isdigit() or len(only_txt) <= 4 or _RUNNING_BANNER_RE.match(only_txt)
                ):
                    continue

            para_acc: list[str] = []

            def _flush_para() -> None:
                nonlocal total_chars
                if not para_acc:
                    return
                merged = ""
                for part in para_acc:
                    if not merged:
                        merged = part
                    elif merged.endswith("-") and len(merged) >= 2 and merged[-2].isalpha() and part[0].islower():
                        merged = merged[:-1] + part
                    else:
                        merged = merged + " " + part
                para_acc.clear()
                total_chars += len(merged)
                md_blocks.append(merged)

            for l_txt, l_sz, l_bold in block_lines:
                is_heading = False
                heading_prefix = "##"

                if page_idx == 0 and l_sz >= body_size + 2.8 and len(l_txt) <= 180:
                    is_heading = True
                    heading_prefix = "#"
                elif l_sz >= body_size + 1.6 and len(l_txt) <= 120 and not l_txt.endswith("."):
                    is_heading = True
                    heading_prefix = "##"
                elif (l_bold or l_sz >= body_size + 0.4) and len(l_txt) <= 100 and not l_txt.endswith("."):
                    if _SUBSECTION_NUMBER_RE.match(l_txt):
                        is_heading = True
                        heading_prefix = "###"
                    elif _SECTION_HEADER_RE.match(l_txt):
                        is_heading = True
                        heading_prefix = "##"

                if is_heading:
                    _flush_para()
                    headings.append(l_txt)
                    total_chars += len(l_txt)
                    md_blocks.append(f"{heading_prefix} {l_txt}")
                elif _LIST_ITEM_RE.match(l_txt):
                    _flush_para()
                    para_acc.append(l_txt)
                else:
                    para_acc.append(l_txt)

            _flush_para()

        # Emit any remaining tables on the page
        for t_idx, (_, t_md) in enumerate(tabs):
            if t_idx not in emitted_tabs:
                md_blocks.append(t_md)
                emitted_tabs.add(t_idx)

    full_text = "\n\n".join(b for b in md_blocks if b.strip())

    # If the PDF has almost no embedded text (< 40 chars/page), it's likely a scanned image PDF
    avg_chars_per_page = total_chars / max(1, len(pages_dict))
    if allow_ocr_fallback and len(pages_dict) > 0 and avg_chars_per_page < 40 and total_chars < 200:
        logger.warning(
            "[PyMuPDF] '%s' appears to be a scanned image PDF (avg %.1f chars/page); falling back to Docling OCR.",
            file_path.name,
            avg_chars_per_page,
        )
        try:
            return extract_pdf_docling(file_path)
        except Exception as e:
            logger.warning("[PyMuPDF] Docling fallback failed for '%s': %s", file_path.name, e)

    report_mcp_progress(
        max(1, len(pages_dict)),
        max(1, len(pages_dict)),
        f"[PyMuPDF] Extracted {len(full_text):,} chars ({len(headings)} headings) from '{file_path.name}'",
    )
    return ExtractedContent(text=full_text, headings=headings)


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
    from src.mcp_servers.progress import report_mcp_progress

    report_mcp_progress(0, 1, f"[Docling] Converting '{file_path.name}' to Markdown...")
    converter = get_converter()
    result = converter.convert(file_path)
    doc = result.document
    
    text = doc.export_to_markdown()
    headings = [line.strip('# ').strip() for line in text.split('\n') if line.startswith('#')]
    report_mcp_progress(1, 1, f"[Docling] Extracted {len(text):,} chars from '{file_path.name}'")
    
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


_NON_RETRYABLE_LITELLM_ERRORS = (
    litellm_exc.BadRequestError,
    litellm_exc.AuthenticationError,
    litellm_exc.NotFoundError,
    litellm_exc.PermissionDeniedError,
)


def _log_ocr_retry(retry_state) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    wait_s = getattr(retry_state.next_action, "sleep", 0.0)
    logger.warning(
        "[LLM OCR Retry] Attempt %d/5 failed (%s: %s) — retrying in %.1fs...",
        retry_state.attempt_number,
        type(exc).__name__ if exc else "Error",
        str(exc).splitlines()[0] if exc else "",
        wait_s,
    )


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_not_exception_type(_NON_RETRYABLE_LITELLM_ERRORS),
    before_sleep=_log_ocr_retry,
    reraise=True,
)
def _ocr_single_page(page_num: int, total_pages: int, img_b64: str, prompt: str, model: str) -> str:
    sem, _ = _get_ocr_semaphore()
    normalized_model = settings.normalize_litellm_model(model, default="openrouter/google/gemini-2.5-flash")
    req_timeout = int(getattr(settings, "LLM_REQUEST_TIMEOUT", 90))
    with sem:
        logger.info("Processing page %d/%d via %s...", page_num + 1, total_pages, normalized_model)
        response = completion(
            model=normalized_model,
            timeout=req_timeout,
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
    from src.mcp_servers.progress import report_mcp_progress

    _, max_workers = _get_ocr_semaphore()
    raw_model = getattr(settings, "VISION_MODEL", settings.DEFAULT_SUMMARIZATION_MODEL)
    model = settings.normalize_litellm_model(raw_model, default="openrouter/google/gemini-2.5-flash")

    prompts = get_prompts()
    prompt_template = prompts["extractor"]["llm_ocr_prompt"]
    prompt = prompt_template.replace("{num_images}", "1")

    # Render pages to base64 JPEGs sequentially on the main thread (PyMuPDF is not thread-safe)
    page_images: list[tuple[int, str]] = []
    with fitz.open(file_path) as doc:
        total_pages = doc.page_count
        report_mcp_progress(
            0,
            max(1, total_pages),
            f"[LLM OCR] Starting Vision OCR on '{file_path.name}' ({total_pages} pages, concurrency={max_workers}, model={model})...",
        )
        for page_num in range(total_pages):
            page = doc.load_page(page_num)
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # 2x resolution
            img_bytes = pix.tobytes("jpeg")
            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
            page_images.append((page_num, img_b64))

    text_parts: list[str] = [""] * len(page_images)
    completed_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {
            executor.submit(_ocr_single_page, p_num, total_pages, b64, prompt, model): p_num
            for p_num, b64 in page_images
        }
        for future in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[future]
            text_parts[idx] = future.result()
            completed_count += 1
            pct = int(completed_count * 100 / max(1, total_pages))
            report_mcp_progress(
                completed_count,
                max(1, total_pages),
                f"[LLM OCR] Completed {completed_count}/{total_pages} pages ({pct}%) for '{file_path.name}'",
            )

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

