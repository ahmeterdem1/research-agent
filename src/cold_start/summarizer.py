import hashlib
import json
from pathlib import Path
from litellm import completion
import sys
import os

sys.path.append(str(Path(__file__).parent.parent.parent))
from config import settings

def get_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def summarize_text(text: str) -> str:
    """
    Summarizes the text using the configured LLM.
    Uses local cache to avoid re-summarization.
    """
    text_hash = get_text_hash(text)
    cache_file = settings.CACHE_DIR / f"{text_hash}.json"
    
    if cache_file.exists():
        return json.loads(cache_file.read_text())["summary"]

    prompt = f"""
    Please provide a concise 2-4 sentence summary of the following document.
    Focus on the main problem, methodology, and key findings.

    Document Text (truncated):
    {text[:15000]}
    """
    
    try:
        response = completion(
            model=settings.DEFAULT_SUMMARIZATION_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )
        summary = response.choices[0].message.content.strip()
        
        # Cache it
        cache_file.write_text(json.dumps({"summary": summary}))
        return summary
    except Exception as e:
        print(f"Summarization error: {e}")
        return "Summary generation failed."

def summarize_directory(children_summaries: list[str]) -> str:
    if not children_summaries:
        return "Empty directory."
        
    prompt = f"""
    Based on the following summaries of documents in this directory, provide a 2-3 sentence overview of what this directory contains.

    Summaries:
    {chr(10).join(children_summaries)}
    """
    
    try:
        response = completion(
            model=settings.DEFAULT_SUMMARIZATION_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Directory summarization error: {e}")
        return "Directory summary generation failed."

