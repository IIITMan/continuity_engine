"""Pure text presentation helpers. They do not touch action payloads or permissions."""
from __future__ import annotations
import re
from typing import Iterable

_MARKDOWN_LINE = re.compile(r"^\s{0,3}(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)")
_AI_PHRASES = ("作为AI助手", "作为人工智能助手", "很高兴为您服务", "作为一个人工智能")

def strip_markdown(text: str) -> str:
    if not isinstance(text, str): raise TypeError("text must be str")
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _MARKDOWN_LINE.sub("", line)
        line = re.sub(r"\*\*(.*?)\*\*|__(.*?)__", lambda m: m.group(1) or m.group(2) or "", line)
        line = re.sub(r"(?<!\*)\*(?!\*)(.*?)\*(?!\*)", r"\1", line)
        line = re.sub(r"`([^`]*)`", r"\1", line)
        out.append(line.strip())
    return "\n".join(x for x in out if x).strip()

def strip_ai_phrases(text: str) -> str:
    """Remove empty boilerplate only; preserve necessary identity/limitation disclosures."""
    if not isinstance(text, str): raise TypeError("text must be str")
    result = text.strip()
    for phrase in _AI_PHRASES:
        result = result.replace(phrase, "")
    result = re.sub(r"[ \t]{2,}", " ", result).strip()
    # Removing a leading boilerplate phrase must not leave its separator punctuation.
    result = re.sub(r"^[ \t]*[，,、;；:：]+[ \t]*", "", result)
    return result

def enforce_length(text: str, limit: int) -> str:
    if not isinstance(text, str): raise TypeError("text must be str")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1: raise ValueError("limit must be a positive integer")
    value = text.strip()
    if len(value) <= limit: return value
    # Prefer a sentence boundary over slicing mid-sentence; if none exists, use an ellipsis.
    window = value[:limit]
    ends = [window.rfind(mark) for mark in ("。", "！", "？", ". ", "! ", "? ", "\n")]
    cut = max(ends)
    if cut >= max(12, int(limit * .55)):
        return window[:cut + (1 if window[cut] in "。！？" else 0)].strip()
    return window[:max(0, limit - 1)].rstrip(" ，、；;:") + "…"

def split_bubbles(text: str, max_chars: int = 100) -> list[str]:
    if not isinstance(text, str): raise TypeError("text must be str")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 10: raise ValueError("max_chars must be >= 10")
    value = text.strip()
    if not value: return []
    segments: list[str] = []
    while len(value) > max_chars:
        candidates = [value.rfind(x, 0, max_chars) for x in ("。", "！", "？", "；", "，", "、")]
        cut = max(candidates)
        if cut < max(10, int(max_chars * .55)): cut = max_chars
        else: cut += 1
        segments.append(value[:cut].strip()); value = value[cut:].strip()
    if value: segments.append(value)
    return segments

def prepare_speech_segments(text: str, *, verbosity_chars: int = 400, bubble_chars: int = 100,
                            remove_markdown: bool = True, remove_boilerplate: bool = True) -> tuple[str, ...]:
    if remove_markdown: text = strip_markdown(text)
    if remove_boilerplate: text = strip_ai_phrases(text)
    text = enforce_length(text, max(1, min(4000, int(verbosity_chars))))
    return tuple(split_bubbles(text, max(10, min(500, int(bubble_chars)))))
