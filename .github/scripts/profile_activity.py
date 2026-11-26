"""Render two self-contained, theme-aware SVG cards from public GitHub feeds."""

import json
import os
import subprocess
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from textwrap import shorten


STYLE = """
text { font: 15px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; fill: #1f2328 }
.heading { font-size: 21px; font-weight: 650 }
.name { font-size: 17px; font-weight: 600 }
.meta { font-size: 14px; fill: #59636e }
.bar { fill: #1f883d }
.rule { stroke: #d1d9e0 }
@media (prefers-color-scheme: dark) {
  text { fill: #f0f6fc } .meta { fill: #a3adb7 }
  .bar { fill: #3fb950 } .rule { stroke: #3d444d }
}
"""


def fetch(endpoint):
    return json.loads(subprocess.check_output(["gh", "api", endpoint]))


def label(x, y, value, style="", anchor="start"):
    return (f'<text x="{x}" y="{y}" class="{style}" text-anchor="{anchor}">'
            f'{escape(str(value))}</text>')


def rule(y):
    return f'<path class="rule" d="M16 {y} H464"/>'


def save(filename, title, body, height):
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="480" height="{height}" '
           f'viewBox="0 0 480 {height}" role="img"><title>{title}</title>',
           f'<style>{STYLE}</style>', *body, '</svg>']
    Path("images", filename).write_bytes(("\n".join(svg) + "\n").encode())


def clipped(value, limit):
    return value if len(value) <= limit else value[:limit - 1] + "…"


def render_activity(owner, events):
    kinds = {"PushEvent": ("push", "pushes"), "PullRequestEvent": ("PR event", "PR events"),
             "IssuesEvent": ("issue event", "issue events"), "PullRequestReviewEvent": ("review", "reviews"),
             "ReleaseEvent": ("release", "releases")}
    groups = Counter()
    weekdays = Counter()
    for event in events:
        repo = event["repo"]["name"]
        kind = event["type"]
        if kind not in kinds or repo.casefold() == f"{owner}/{owner}".casefold():
            continue
        day = datetime.fromisoformat(event["created_at"]).astimezone(timezone(timedelta(hours=7))).date()
        groups[day, repo, kind] += 1
        if kind == "PushEvent":
            weekdays[day.weekday()] += 1

    body = [label(16, 32, "Pushes by weekday", "heading"),
            label(16, 56, f"Latest {len(events)} public events · UTC+7", "meta"), rule(152)]
    peak = max(weekdays.values(), default=1)
    for i, day in enumerate("Mon Tue Wed Thu Fri Sat Sun".split()):
        x = 48 + 64 * i
        count = weekdays[i]
        height = round(67 * count / peak)
        body.extend([f'<rect class="bar" x="{x-13}" y="{152-height}" width="26" height="{height}" rx="3"/>',
                     label(x, 145-height, count, anchor="middle"), label(x, 173, day, "meta", "middle")])

    body.append(label(16, 218, "Recent activity", "heading"))
    y = 252
    for (day, repo, kind), count in list(groups.items())[:6]:
        namespace, name = repo.split("/", 1)
        body.extend([label(16, y, clipped(name, 31), "name"),
                     label(464, y, day.strftime("%d %b"), "meta", "end"),
                     label(16, y+21, f"{clipped(namespace, 28)} · {count} {kinds[kind][count != 1]}", "meta"),
                     rule(y+32)])
        y += 58
    if not groups:
        body.append(label(16, y, "No matching public activity.", "meta"))
        y += 34
    token = os.environ.get("WAKATIME_TOKEN")
    if token:
        try:
            y = append_wakatime(body, y, token)
        except (URLError, TimeoutError, ValueError, KeyError) as error:
            print(f"::warning::Weekly WakaTime stats unavailable ({getattr(error, 'code', type(error).__name__)}).")
    save("metrics-activity.svg", "Pushes by weekday and recent activity", body, y-8)


def append_wakatime(body, y, token):
    request = Request("https://wakatime.com/api/v1/users/current/stats/last_7_days",
                      headers={"Authorization": f"Bearer {token}"})
    with urlopen(request, timeout=15) as response:
        stats = json.load(response)["data"]
    panel = [label(16, y+16, "WakaTime · last 7 days", "heading"),
             label(16, y+42, stats["human_readable_total"], "meta")]
    y += 76
    for section in ("projects", "languages"):
        rows = (stats.get(section) or [])[:5]
        if rows:
            panel.append(label(16, y, section.title(), "name"))
            y += 24
        for row in rows:
            percent = max(0, min(100, float(row["percent"])))
            panel.extend([label(16, y, clipped(row["name"], 24)),
                          f'<rect class="bar" x="242" y="{y-10}" width="{percent*1.5:.1f}" height="8" rx="4"/>',
                          label(464, y, f"{percent:g}%", "meta", "end")])
            y += 24
    body.extend(panel)
    return y


def render_stars(stars):
    body = [label(16, 32, "Recently starred", "heading")]
    y = 68
    for repo in stars:
        body.append(label(16, y, clipped(repo["name"], 34), "name"))
        metadata = [repo["owner"]["login"], repo.get("language"), f'{repo["stargazers_count"]:,} stars']
        body.append(label(16, y+22, " · ".join(item for item in metadata if item), "meta"))
        if repo.get("description"):
            y += 22
            body.append(label(16, y+22, shorten(repo["description"], width=54, placeholder="…")))
        body.append(rule(y+36))
        y += 62
    if not stars:
        body.append(label(16, y, "No public stars yet.", "meta"))
        y += 32
    save("metrics-stars.svg", "Recently starred repositories", body, y-8)


owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "wikiepeidia")
render_activity(owner, fetch(f"users/{owner}/events/public?per_page=100"))
render_stars(fetch(f"users/{owner}/starred?sort=created&direction=desc&per_page=4"))
