"""Render profile cards from the public GitHub REST feeds."""

import json
import os
import subprocess
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path


def fetch(endpoint):
    return json.loads(subprocess.check_output(["gh", "api", endpoint]))


def card(filename, title, rows):
    height = 53 + len(rows) * 24
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 {height}" width="480" height="{height}">',
        '<style>text{font:14px system-ui,sans-serif;fill:#24292f}.heading{font-weight:650;fill:#0969da}'
        '@media(prefers-color-scheme:dark){text{fill:#e6edf3}.heading{fill:#58a6ff}}</style>',
        f'<text class="heading" x="12" y="23">{escape(title)}</text>',
    ]
    for index, row in enumerate(rows):
        label = row if len(row) <= 54 else row[:53] + "…"
        svg.append(f'<text x="12" y="{50 + 24 * index}"><title>{escape(row)}</title>{escape(label)}</text>')
    Path("images", filename).write_bytes(("\n".join(svg) + "\n</svg>\n").encode())


owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "wikiepeidia")
events = fetch(f"users/{owner}/events/public?per_page=100")
labels = {"PushEvent": "Push", "PullRequestEvent": "PR", "IssuesEvent": "Issue",
          "PullRequestReviewEvent": "Review", "ReleaseEvent": "Release"}
recent = []
weekdays = Counter()
for event in events:
    repo = event["repo"]["name"]
    kind = event["type"]
    if repo.lower() == f"{owner}/{owner}".lower() or kind not in labels:
        continue
    local = datetime.fromisoformat(event["created_at"]).astimezone(timezone(timedelta(hours=7)))
    if kind == "PushEvent":
        weekdays[local.weekday()] += 1
    if len(recent) < 6:
        recent.append(f'{local:%d %b} · {labels[kind]} · {repo}')

counts = "   ".join(f"{day} {weekdays[i]}" for i, day in enumerate("Mo Tu We Th Fr Sa Su".split()))
card("metrics-activity.svg", "Pushes in the latest 100 public events", [counts, *recent])
stars = fetch(f"users/{owner}/starred?sort=created&direction=desc&per_page=4")
card("metrics-stars.svg", "Recently starred repositories", [r["full_name"] for r in stars] or ["No public stars"])
