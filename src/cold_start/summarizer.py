import hashlib
import json
from pathlib import Path
from litellm import completion
from tenacity import retry, stop_after_attempt, wait_exponential
import sys
import os

sys.path.append(str(Path(__file__).parent.parent.parent))
from config import settings
from config.prompts import get_prompts

def get_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
def summarize_text(text: str) -> str:
    """
    Summarizes the text using the configured LLM.
    Uses local cache to avoid re-summarization.
    """
    text_hash = get_text_hash(text)
    cache_file = settings.CACHE_DIR / f"{text_hash}.json"
    
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))["summary"]

    prompts = get_prompts()
    prompt_template = prompts["summarizer"]["document_prompt"]
    prompt = prompt_template.format(text=text[:15000])
    
    try:
        response = completion(
            model=settings.DEFAULT_SUMMARIZATION_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )
        summary = response.choices[0].message.content.strip()
        
        # Cache it
        cache_file.write_text(json.dumps({"summary": summary}), encoding="utf-8")
        return summary
    except Exception as e:
        print(f"Summarization error: {e}")
        return "Summary generation failed."

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
def summarize_directory(children_summaries: list[str]) -> str:
    if not children_summaries:
        return "Empty directory."
        
    prompts = get_prompts()
    prompt_template = prompts["summarizer"]["directory_prompt"]
    prompt = prompt_template.format(summaries=chr(10).join(children_summaries))
    
    try:
        response = completion(
            model=settings.DEFAULT_SUMMARIZATION_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Directory summarization error: {e}")
        return "Directory summary generation failed."

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
def summarize_book(children_summaries: list[str]) -> str:
    if not children_summaries:
        return "Empty book."
        
    prompts = get_prompts()
    prompt_template = prompts["summarizer"]["book_prompt"]
    prompt = prompt_template.format(summaries=chr(10).join(children_summaries))
    
    try:
        response = completion(
            model=settings.DEFAULT_SUMMARIZATION_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Book summarization error: {e}")
        return "Book summary generation failed."

