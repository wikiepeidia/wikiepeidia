"""Generate a daily funny AI programming quote SVG via Groq and OpenRouter."""

import json
import os
import random
import re
import subprocess
from difflib import SequenceMatcher
from xml.etree import ElementTree
from html import escape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = """Write one original English programming joke for wikiepeidia's GitHub profile.
Pick one specific thing a developer did and the unexpected consequence. Put the funniest detail last.
Dry, petty, or absurd is fine. Skip stock jokes about caffeine, Friday deploys, and bugs being features.
Code is optional. Use {codeblock: ...} for one short inline snippet only when the literal code is the punchline.
Keep the quote under 170 characters. Give it a short fictional byline that fits the joke, such as the object speaking.
Return only a JSON object with two strings: {"quote":"...","author":"..."}. No explanation or Markdown fences."""

USER_PROMPT = "Write today's quote. Use a different premise and punchline from the recent quotes below."

GROQ_MODELS = (
    ("openai/gpt-oss-120b", {"max_completion_tokens": 512, "reasoning_effort": "low"}),
    ("openai/gpt-oss-20b", {"max_completion_tokens": 512, "reasoning_effort": "low"}),
    (os.environ.get("GROQ_QWEN_MODEL") or "qwen/qwen3.8-27b", {"max_completion_tokens": 512}),
    ("llama-3.3-70b-versatile", {"max_completion_tokens": 256}),
)


def recent_quotes():
    """Read the titles of the last eight published cards, including older SVGs."""
    ref = "refs/remotes/origin/ai-quote-output"
    commits = subprocess.run(["git", "log", "-8", "--format=%H", ref, "--", "images/ai-quote.svg"],
                             capture_output=True)
    history = []
    for commit in commits.stdout.decode("ascii").splitlines():
        blob = subprocess.run(["git", "show", f"{commit}:images/ai-quote.svg"], capture_output=True)
        if blob.returncode == 0:
            try:
                title = ElementTree.fromstring(blob.stdout).findtext("{http://www.w3.org/2000/svg}title")
            except ElementTree.ParseError:
                continue
            if title:
                history.append(title.rsplit(" — ", 1)[0][:220])
    return history


def comparable_quote(text):
    visible = "".join(value for _, value in tokenize_quote(text))
    return " ".join(re.findall(r"\w+", visible.casefold()))


FALLBACK_QUOTES = [
    (
        "I refactored the legacy code at 3 AM and now both God and git blame have left the chat.",
        "I am an AI - Emergency Fallback Toaster",
    ),
    (
        "{codeblock: 1 + '1'} is peak engineering until production catches fire.",
        "I am an AI - Schrödinger's Linter",
    ),
    (
        "Deploying {codeblock: git push --force} on Friday builds character and emergency rollbacks.",
        "I am an AI - Rate-Limited Oracle",
    ),
]

STYLE = """
text { font: italic 16px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; fill: #1f2328 }
.surface { fill: #ffffff; stroke: #d1d9e0; stroke-width: 1 }
.mark { font: normal 700 22px Georgia, 'Times New Roman', serif; fill: #2563eb }
.code { font: normal 600 14.5px ui-monospace, 'Cascadia Code', Consolas, 'Liberation Mono', monospace; fill: #b45309 }
.author { font: italic 15px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; fill: #2563eb }
@media (prefers-color-scheme: dark) {
  text { fill: #e6edf3 }
  .surface { fill: #0d1117; stroke: #30363d }
  .mark, .author { fill: #58a6ff }
  .code { fill: #ffa657 }
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


def _strip_think_tags(text):
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()
    # If a reasoning model was truncated mid-<think>, discard the incomplete thought block
    return re.sub(r"<think>[\s\S]*$", "", cleaned, flags=re.IGNORECASE).strip()


def _looks_like_json(candidate):
    if candidate.startswith("["):
        return True
    if candidate.startswith("{") and not re.match(r"^\{codeblock\s*:", candidate, re.IGNORECASE):
        return True
    return bool(re.search(r'"quote"\s*:', candidate))


def parse_quote_payload(raw_content, model_name=""):
    """Extract (quote, author) from strict JSON, fenced JSON, or plain text; reject malformed JSON."""
    text = _strip_think_tags(str(raw_content or "").strip())
    if not text:
        return None
    if text.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    else:
        fenced = re.search(r"```json\s*([\s\S]*?)```", text, re.IGNORECASE)
        candidate = fenced.group(1).strip() if fenced else text
    if not candidate:
        return None

    if _looks_like_json(candidate):
        match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", candidate)
        if not match:
            return None
        try:
            obj = json.loads(match.group(1))
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        raw_quote = obj.get("quote")
        if not isinstance(raw_quote, str):
            return None
        quote = _strip_quotes(raw_quote)
        if not quote:
            return None
        raw_author = obj.get("author")
        author = _strip_quotes(raw_author) if isinstance(raw_author, str) else ""
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


def tokenize_quote(text):
    """Split text into (is_code, value) segments from {codeblock: ...} and `...` spans."""
    segments = []
    pos = 0
    buf = []
    length = len(text)
    while pos < length:
        codeblock = re.match(r"\{codeblock\s*:", text[pos:], re.IGNORECASE)
        if codeblock:
            depth = 1
            cursor = pos + codeblock.end()
            while cursor < length and depth > 0:
                if text[cursor] == "{":
                    depth += 1
                elif text[cursor] == "}":
                    depth -= 1
                cursor += 1
            if depth == 0:
                inner = " ".join(text[pos + codeblock.end():cursor - 1].split()).strip()
                if buf:
                    segments.append((False, "".join(buf)))
                    buf.clear()
                if inner:
                    segments.append((True, inner))
                pos = cursor
                continue
        if text[pos] == "`":
            end = text.find("`", pos + 1)
            if end != -1:
                inner = " ".join(text[pos + 1:end].split()).strip()
                if buf:
                    segments.append((False, "".join(buf)))
                    buf.clear()
                if inner:
                    segments.append((True, inner))
                pos = end + 1
                continue
        buf.append(text[pos])
        pos += 1
    if buf:
        segments.append((False, "".join(buf)))
    return segments or [(False, text)]


def _wrap_segments(segments, max_chars=66):
    """Wrap (is_code, value) segments into SVG lines preserving code spans."""
    tokens = []
    for is_code, value in segments:
        if is_code:
            tokens.append((True, value))
        else:
            for part in re.findall(r"\S+|\s+", value):
                tokens.append((False, part))

    lines = []
    current = []
    line_len = 0

    def flush():
        nonlocal current, line_len
        while current and not current[-1][0] and not current[-1][1].strip():
            current.pop()
        if current:
            lines.append(current)
        current = []
        line_len = 0

    for is_code, token in tokens:
        if not is_code and not token.strip():
            if current and line_len < max_chars:
                current.append((False, " "))
                line_len += 1
            continue
        if len(token) > max_chars:
            if current:
                flush()
            for start in range(0, len(token), max_chars):
                chunk = token[start:start + max_chars]
                lines.append([(is_code, chunk)])
            continue
        needed = len(token)
        if current and line_len + needed > max_chars:
            flush()
        current.append((is_code, token))
        line_len += needed
    flush()
    return lines or [[(False, "")]]


def call_provider(name, url, api_key, model, extra_payload=None, history=()):
    """Call an OpenAI-compatible chat completions endpoint and return (quote, author)."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT + "\n" + json.dumps(list(history), ensure_ascii=False)},
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


def fetch_ai_quote(rng=None, history=()):
    """Try Groq and OpenRouter with automatic failover and safe local fallback."""
    rng = rng or random
    candidates = []
    groq_key = (os.environ.get("GROK_API_KEY") or os.environ.get("GROQ_API_KEY") or "").strip()
    openrouter_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if groq_key:
        primary, backup = rng.sample(GROQ_MODELS, 2)
        candidates.append(("groq", GROQ_URL, groq_key, *primary))
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
    if groq_key:
        candidates.append(("groq-fallback", GROQ_URL, groq_key, *backup))

    for name, url, key, model, extra in candidates:
        try:
            quote, author = call_provider(name, url, key, model, extra, history)
            normalized = comparable_quote(quote)
            if any(SequenceMatcher(None, normalized, comparable_quote(old)).ratio() >= 0.86 for old in history):
                raise ValueError("Repeated quote")
            return quote, author, True
        except HTTPError as error:
            hint = " — key may be expired" if error.code in (401, 403) else ""
            print(f"::warning::AI quote provider {name} failed (HTTP {error.code}{hint}).")
        except (URLError, TimeoutError, ValueError, KeyError, TypeError) as error:
            print(f"::warning::AI quote provider {name} failed ({type(error).__name__}).")

    print("::warning::All AI quote providers unavailable; using built-in fallback quote.")
    quote, author = rng.choice(FALLBACK_QUOTES)
    return quote, author, False


def render_quote_svg(quote, author, output_path=Path("images/ai-quote.svg")):
    """Render a light/dark-adaptive SVG quote card with inline code styling."""
    clean_quote = (_strip_quotes(quote) or FALLBACK_QUOTES[0][0])[:220]
    clean_author = (_strip_quotes(author) or FALLBACK_QUOTES[0][1])[:80]
    segments = tokenize_quote(clean_quote)
    plain_title = "".join(val for _, val in segments)
    lines = _wrap_segments(segments, max_chars=66)
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
        f'<title>{escape(plain_title)} — {escape(clean_author)}</title>',
        f'<style>{STYLE}</style>',
        f'<rect class="surface" x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="10"/>',
    ]

    for idx, line_tokens in enumerate(lines):
        y = start_y + idx * line_height
        spans = []
        if idx == 0:
            spans.append('<tspan class="mark">“</tspan>')
        for is_code, part in line_tokens:
            if is_code:
                spans.append(f'<tspan class="code">{escape(part)}</tspan>')
            else:
                spans.append(escape(part))
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
    quote, author, from_api = fetch_ai_quote(history=recent_quotes())
    render_quote_svg(quote, author)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf8") as output:
            print(f"from_api={'true' if from_api else 'false'}", file=output)


if __name__ == "__main__":
    main()
