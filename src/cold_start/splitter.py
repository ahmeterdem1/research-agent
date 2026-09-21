from pathlib import Path
from dataclasses import dataclass
from src.cold_start.scanner import FileInfo
from src.cold_start.extractor import ExtractedContent

import fitz
import os

@dataclass
class SplitResult:
    is_split: bool
    sections: list[FileInfo]
    warning: str | None = None

def split_book(file_info: FileInfo, limit: int = 30) -> SplitResult:
    """
    Attempts to split a book into chapters using its TOC.
    Recursively splits using deeper TOC levels if sections exceed the limit.
    Falls back to hard splitting by page limit if no TOC subheadings exist.
    """
    import sys
    sys.path.append(str(Path(__file__).parent.parent.parent))
    from config import settings
    
    try:
        with fitz.open(file_info.real_path) as doc:
            toc = doc.get_toc()
            if not toc:
                return SplitResult(is_split=False, sections=[], warning="No TOC found.")
                
            total_pages = doc.page_count
            
            def chunk_range(s, e, base_title, lim):
                chunks = []
                curr = s
                part = 1
                while curr <= e:
                    nxt = min(curr + lim - 1, e)
                    t = f"{base_title} Part {part}" if (e - s + 1 > lim) else base_title
                    chunks.append((curr, nxt, t))
                    curr = nxt + 1
                    part += 1
                return chunks
                
            def process_range(start_page, end_page, title, subset_toc, current_level):
                size = end_page - start_page + 1
                if size <= limit:
                    return [(start_page, end_page, title)]
                    
                headings = [h for h in subset_toc if h[0] == current_level and start_page <= h[2] <= end_page]
                
                if not headings:
                    deeper = [h for h in subset_toc if h[0] > current_level and start_page <= h[2] <= end_page]
                    if deeper:
                        next_level = min(h[0] for h in deeper)
                        return process_range(start_page, end_page, title, subset_toc, next_level)
                    else:
                        return chunk_range(start_page, end_page, title, limit)
                        
                chunks = []
                if headings[0][2] > start_page:
                    chunks.extend(process_range(start_page, headings[0][2] - 1, f"{title} (Intro)", subset_toc, current_level + 1))
                    
                for i, h in enumerate(headings):
                    h_start = h[2]
                    h_title = h[1]
                    h_end = headings[i+1][2] - 1 if i + 1 < len(headings) else end_page
                    
                    if h_start <= h_end:
                        chunks.extend(process_range(h_start, h_end, h_title, subset_toc, current_level + 1))
                        
                return chunks

            final_chunks = process_range(1, total_pages, file_info.name, toc, 1)
            
            # Now we have a list of (start_page, end_page, title)
            # We save each as a new PDF
            sections = []
            book_cache_dir = settings.CACHE_DIR / "split_books" / file_info.name
            book_cache_dir.mkdir(parents=True, exist_ok=True)
            
            print(f"Splitting {file_info.name} into {len(final_chunks)} sections with page counts {[e - s + 1 for s, e, _ in final_chunks]}")
            
            for idx, (start_p, end_p, title) in enumerate(final_chunks):
                sec_doc = fitz.open()
                sec_doc.insert_pdf(doc, from_page=start_p-1, to_page=end_p-1)
                
                safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip()
                if not safe_title:
                    safe_title = f"Section_{idx+1}"
                    
                sec_path = book_cache_dir / f"{idx+1:02d}_{safe_title}.pdf"
                sec_doc.save(str(sec_path))
                sec_doc.close()
                
                sections.append(FileInfo(
                    real_path=str(sec_path),
                    name=safe_title,
                    file_type="pdf",
                    page_count=end_p - start_p + 1,
                    is_book=False,
                    is_splittable=False
                ))
                
        return SplitResult(is_split=True, sections=sections)
    except Exception as e:
        return SplitResult(is_split=False, sections=[], warning=str(e))

