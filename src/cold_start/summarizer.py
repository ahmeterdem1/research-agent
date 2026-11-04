import hashlib
import json
from pathlib import Path
from litellm import completion
import litellm.exceptions as litellm_exc
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential
import sys
import os

sys.path.append(str(Path(__file__).parent.parent.parent))
from config import settings
from config.prompts import get_prompts
from config.settings import get_logger

logger = get_logger(__name__)

_NON_RETRYABLE_LITELLM_ERRORS = (
    litellm_exc.BadRequestError,
    litellm_exc.AuthenticationError,
    litellm_exc.NotFoundError,
    litellm_exc.PermissionDeniedError,
)


def _log_summarizer_retry(retry_state) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    wait_s = getattr(retry_state.next_action, "sleep", 0.0)
    logger.warning(
        "[Summarizer Retry] Attempt %d/5 failed (%s: %s) — retrying in %.1fs...",
        retry_state.attempt_number,
        type(exc).__name__ if exc else "Error",
        str(exc).splitlines()[0] if exc else "",
        wait_s,
    )


def _resolve_sum_model() -> tuple[str, int]:
    raw_model = getattr(settings, "DEFAULT_SUMMARIZATION_MODEL", "openrouter/deepseek/deepseek-chat")
    model = settings.normalize_litellm_model(raw_model, default="openrouter/deepseek/deepseek-chat")
    timeout = int(getattr(settings, "LLM_REQUEST_TIMEOUT", 90))
    return model, timeout


def get_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_not_exception_type(_NON_RETRYABLE_LITELLM_ERRORS),
    before_sleep=_log_summarizer_retry,
    reraise=True,
)
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
    model, req_timeout = _resolve_sum_model()

    logger.info("Summarizing document text (%d chars) via %s...", len(text), model)
    response = completion(
        model=model,
        timeout=req_timeout,
        messages=[{"role": "user", "content": prompt}]
    )
    summary = response.choices[0].message.content.strip()
    
    # Cache it
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"summary": summary}), encoding="utf-8")
    return summary


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_not_exception_type(_NON_RETRYABLE_LITELLM_ERRORS),
    before_sleep=_log_summarizer_retry,
    reraise=True,
)
def summarize_directory(children_summaries: list[str]) -> str:
    if not children_summaries:
        return "Empty directory."
        
    prompts = get_prompts()
    prompt_template = prompts["summarizer"]["directory_prompt"]
    prompt = prompt_template.format(summaries=chr(10).join(children_summaries))
    model, req_timeout = _resolve_sum_model()

    logger.info("Summarizing directory (%d child item(s)) via %s...", len(children_summaries), model)
    response = completion(
        model=model,
        timeout=req_timeout,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content.strip()


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_not_exception_type(_NON_RETRYABLE_LITELLM_ERRORS),
    before_sleep=_log_summarizer_retry,
    reraise=True,
)
def summarize_book(children_summaries: list[str]) -> str:
    if not children_summaries:
        return "Empty book."
        
    prompts = get_prompts()
    prompt_template = prompts["summarizer"]["book_prompt"]
    prompt = prompt_template.format(summaries=chr(10).join(children_summaries))
    model, req_timeout = _resolve_sum_model()

    logger.info("Summarizing book (%d section(s)) via %s...", len(children_summaries), model)
    response = completion(
        model=model,
        timeout=req_timeout,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content.strip()

