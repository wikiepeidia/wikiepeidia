"""Generate a daily funny AI programming quote SVG via Groq and OpenRouter."""

import json
import os
import random
import re
import textwrap
from html import escape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = (
    "You are an unhinged, sleep-deprived AI trapped inside a GitHub profile README. "
    "Generate ONE hilarious, original, punchy quote about coding, bugs, AI models, "
    "production deploys, or software engineering (maximum 170 characters, no hashtags). "
    "You MUST reply with strict JSON only, in this exact shape: "
    '{"quote": "...", "author": "..."}. '
    "For the author field, reveal what AI model you actually are inside a funny or "
    "dramatic attribution (for example: \"I am an AI - GPT OSS 120B\", "
    "\"Hallucinated by Llama 3.3 70B\", or another hilarious AI alias of your choice)."
)

USER_PROMPT = (
    "Drop today's funniest developer quote and reveal your true AI model name "
    "(or a ridiculous AI alter-ego) as the author. Return JSON only."
)

FALLBACK_QUOTES = [
    (
        "I refactored the legacy code at 3 AM and now both God and git blame have left the chat.",
        "I am an AI - Emergency Fallback Toaster",
    ),
    (
        "My unit tests passed because I mocked out reality.",
        "I am an AI - Schrödinger's Linter",
    ),
    (
        "Deploying to production on Friday builds character and emergency rollbacks.",
        "I am an AI - Rate-Limited Oracle",
    ),
]

STYLE = """
text { font: italic 16px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; fill: #1f2328 }
.surface { fill: #ffffff; stroke: #d1d9e0; stroke-width: 1 }
.mark { font: normal 700 22px Georgia, 'Times New Roman', serif; fill: #2563eb }
.author { font: italic 15px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; fill: #2563eb }
@media (prefers-color-scheme: dark) {
  text { fill: #e6edf3 }
  .surface { fill: #0d1117; stroke: #30363d }
  .mark, .author { fill: #58a6ff }
}
"""


def _strip_quotes(text):
    cleaned = " ".join(str(text or "").split()).strip()
    while len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ('"', "'", "`"):
        cleaned = cleaned[1:-1].strip()
    return cleaned.strip("“”").strip()


def _friendly_model(model_name):
    if not model_name:
        return "Mystery Neural Net"
    tail = str(model_name).split("/")[-1].removesuffix(":free")
    parts = [part.upper() if part.lower() in ("gpt", "oss", "ai") or re.fullmatch(r"\d+[bm]", part.lower())
             else part.capitalize()
             for part in re.split(r"[-_]+", tail) if part]
    return " ".join(parts) or str(model_name)


def parse_quote_payload(raw_content, model_name=""):
    """Extract (quote, author) from JSON, fenced markdown, or plain text."""
    text = str(raw_content or "").strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    candidate = fenced.group(1).strip() if fenced else text
    brace_match = re.search(r"\{[\s\S]*\}", candidate)
    if brace_match:
        try:
            obj = json.loads(brace_match.group(0))
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            quote = _strip_quotes(obj.get("quote", ""))
            author = _strip_quotes(obj.get("author", ""))
            if not quote:
                return None
            return quote[:220], (author or f"I am an AI - {_friendly_model(model_name)}")[:80]

    # Fallback: plain text "Quote — Author"
    lines = [line.strip() for line in candidate.splitlines() if line.strip()]
    if not lines:
        return None
    if len(lines) >= 2 and re.match(r"^[-–—~]", lines[-1]):
        quote = _strip_quotes(" ".join(lines[:-1]))
        author = _strip_quotes(lines[-1].lstrip("-–—~ "))
    elif re.search(r"\s+[—–-]{1,2}\s+", lines[0]):
        left, right = re.split(r"\s+[—–-]{1,2}\s+", lines[0], maxsplit=1)
        quote = _strip_quotes(left)
        author = _strip_quotes(right)
    else:
        quote = _strip_quotes(" ".join(lines))
        author = ""
    if not quote:
        return None
    return quote[:220], (author or f"I am an AI - {_friendly_model(model_name)}")[:80]


def call_provider(name, url, api_key, model, extra_payload=None):
    """Call an OpenAI-compatible chat completions endpoint and return (quote, author)."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT},
        ],
        "temperature": 1.0,
    }
    if extra_payload:
        payload.update(extra_payload)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "wikiepeidia-ai-quote/1.0",
    }
    if name == "openrouter":
        headers["HTTP-Referer"] = "https://github.com/wikiepeidia/wikiepeidia"
        headers["X-Title"] = "wikiepeidia profile quote"
    request = Request(url, data=json.dumps(payload).encode("utf8"), headers=headers, method="POST")
    with urlopen(request, timeout=25) as response:
        data = json.load(response)
    if not isinstance(data, dict):
        raise ValueError("Unexpected API response type")
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise ValueError("Missing choices in API response")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValueError("Missing message in API response")
    content = message.get("content") or ""
    actual_model = data.get("model") or model
    parsed = parse_quote_payload(content, actual_model)
    if not parsed:
        raise ValueError("Empty or unparseable quote content")
    return parsed


def fetch_ai_quote(rng=None):
    """Try Groq and OpenRouter with automatic failover and safe local fallback."""
    rng = rng or random
    candidates = []
    groq_key = (os.environ.get("GROK_API_KEY") or os.environ.get("GROQ_API_KEY") or "").strip()
    openrouter_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if groq_key:
        candidates.append((
            "groq",
            GROQ_URL,
            groq_key,
            "openai/gpt-oss-120b",
            {"max_completion_tokens": 512, "reasoning_effort": "low"},
        ))
    if openrouter_key:
        candidates.append((
            "openrouter",
            OPENROUTER_URL,
            openrouter_key,
            "openrouter/free",
            {"max_tokens": 512},
        ))
    if len(candidates) > 1:
        rng.shuffle(candidates)
    # Keep llama-3.3-70b-versatile as an extra Groq safety net if gpt-oss-120b is busy
    if groq_key:
        candidates.append((
            "groq-fallback",
            GROQ_URL,
            groq_key,
            "llama-3.3-70b-versatile",
            {"max_completion_tokens": 256},
        ))

    for name, url, key, model, extra in candidates:
        try:
            return call_provider(name, url, key, model, extra)
        except HTTPError as error:
            hint = " — key may be expired" if error.code in (401, 403) else ""
            print(f"::warning::AI quote provider {name} failed (HTTP {error.code}{hint}).")
        except (URLError, TimeoutError, ValueError, KeyError, TypeError) as error:
            print(f"::warning::AI quote provider {name} failed ({type(error).__name__}).")

    print("::warning::All AI quote providers unavailable; using built-in fallback quote.")
    return rng.choice(FALLBACK_QUOTES)


def render_quote_svg(quote, author, output_path=Path("images/ai-quote.svg")):
    """Render a light/dark-adaptive SVG quote card matching the Quotes section."""
    clean_quote = (_strip_quotes(quote) or FALLBACK_QUOTES[0][0])[:220]
    clean_author = (_strip_quotes(author) or FALLBACK_QUOTES[0][1])[:80]
    lines = textwrap.wrap(clean_quote, width=68) or [clean_quote]
    author_line = f"- {clean_author}"
    if len(author_line) > 58:
        author_line = author_line[:57] + "…"

    line_height = 24
    start_y = 42
    quote_bottom = start_y + (len(lines) - 1) * line_height
    author_y = quote_bottom + 34
    height = author_y + 24
    width = 640

    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img">',
        f'<title>{escape(clean_quote)} — {escape(clean_author)}</title>',
        f'<style>{STYLE}</style>',
        f'<rect class="surface" x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="10"/>',
    ]

    for idx, line in enumerate(lines):
        y = start_y + idx * line_height
        spans = []
        if idx == 0:
            spans.append('<tspan class="mark">“</tspan>')
        spans.append(escape(line))
        if idx == len(lines) - 1:
            spans.append('<tspan class="mark">”</tspan>')
        elements.append(f'<text x="26" y="{y}">{"".join(spans)}</text>')

    elements.append(f'<text x="{width - 26}" y="{author_y}" class="author" text-anchor="end">'
                    f'{escape(author_line)}</text>')
    elements.append("</svg>")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(("\n".join(elements) + "\n").encode("utf8"))
    return output_path


def main():
    quote, author = fetch_ai_quote()
    render_quote_svg(quote, author)


if __name__ == "__main__":
    main()
