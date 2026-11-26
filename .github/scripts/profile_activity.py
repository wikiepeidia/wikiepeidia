"""Render two self-contained, theme-aware SVG cards from public GitHub feeds."""

import json
import os
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from time import sleep
from urllib.error import URLError
from urllib.request import Request, urlopen
from textwrap import wrap


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


def public_contributions(owner):
    fields = {"commits": "commit", "issues": "issue", "PRs": "pullRequest",
              "reviews": "pullRequestReview"}
    selections = " ".join(
        f"{label}: {field}ContributionsByRepository(maxRepositories: 100) "
        "{ repository { nameWithOwner isPrivate } contributions { totalCount } }"
        for label, field in fields.items()
    )
    query = ("query($login:String!) { user(login:$login) { contributionsCollection { "
             + selections + " } } }")
    result = json.loads(subprocess.check_output(
        ["gh", "api", "graphql", "-f", f"query={query}", "-f", f"login={owner}"]))
    counts = defaultdict(Counter)
    for kind, rows in result["data"]["user"]["contributionsCollection"].items():
        for row in rows:
            if not row["repository"]["isPrivate"]:
                counts[row["repository"]["nameWithOwner"]][kind] = row["contributions"]["totalCount"]
    return counts


def render_activity(events, contributions):
    recent = Counter()
    created_repos = set()
    weekdays = Counter()
    for event in sorted(events, key=lambda item: item["created_at"], reverse=True):
        repo = event["repo"]["name"]
        kind = event["type"]
        recent[repo] += 1
        if kind == "CreateEvent" and event["payload"].get("ref_type") == "repository":
            created_repos.add(repo)
        day = datetime.fromisoformat(event["created_at"]).astimezone(timezone(timedelta(hours=7))).date()
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

    body.append(label(16, 218, "Recently active repositories", "heading"))
    body.append(label(16, 243, "Past year’s contributions · recent public event counts", "meta"))
    y = 280
    # First appearance records the newest event for each distinct repository.
    repos = list(recent)[:6]
    for repo in repos:
        details = [f"{count} {kind}" for kind, count in contributions.get(repo, {}).items() if count]
        if repo in created_repos:
            details.insert(0, "Created repository")
        details.append(f"{recent[repo]} recent event{'s' if recent[repo] != 1 else ''}")
        for line in wrap(repo, width=41):
            body.append(label(16, y, line, "name"))
            y += 22
        for line in wrap(" · ".join(details), width=58):
            body.append(label(16, y, line, "meta"))
            y += 20
        body.append(rule(y-6))
        y += 20
    if not repos:
        body.append(label(16, y, "No public activity in these periods.", "meta"))
        y += 34
    save("metrics-activity.svg", "Pushes by weekday and recent activity", body, y-8)


def waka_data(endpoint, token):
    request = Request(f"https://wakatime.com/api/v1/users/current/{endpoint}",
                      headers={"Authorization": f"Bearer {token}"})
    with urlopen(request, timeout=15) as response:
        return json.load(response)["data"]


def duration(seconds):
    minutes = int(seconds) // 60
    if minutes >= 60:
        return f"{minutes // 60}h {minutes % 60}m"
    return f"{minutes} min" if minutes else f"{int(seconds)} sec"


def render_wakatime():
    title = "WakaTime · all time"
    body = [label(16, 32, title, "heading")]
    token = os.environ.get("WAKATIME_TOKEN")
    y = 68
    if not token:
        body.append(label(16, y, "Waiting for WakaTime access.", "meta"))
    else:
        try:
            total = waka_data("all_time_since_today", token)
            seconds = total.get("total_seconds")
            if seconds is not None and (seconds or total.get("is_up_to_date", True)):
                body.append(label(16, y, duration(seconds) + " tracked since joining", "name"))
                y += 24
            if total.get("is_up_to_date") is False:
                body.append(label(16, y, "WakaTime is updating the lifetime total.", "meta"))
                y += 24
        except (URLError, TimeoutError, ValueError, KeyError) as error:
            print(f"::warning::Lifetime total unavailable ({type(error).__name__}).")
        try:
            today = datetime.now(timezone(timedelta(hours=7))).date()
            days = waka_data(f"summaries?start={today-timedelta(days=6)}&end={today}&timezone=Asia%2FHo_Chi_Minh", token)
            times = [(day["range"]["date"], day["grand_total"]["total_seconds"]) for day in days]
            peak = max((seconds for _, seconds in times), default=0) or 1
            if times:
                y = waka_bars(body, y, "Last 7 days · UTC+7",
                              [(date, seconds*100/peak, duration(seconds)) for date, seconds in times])
        except (URLError, TimeoutError, ValueError, KeyError) as error:
            print(f"::warning::Daily WakaTime summaries unavailable ({type(error).__name__}).")
        try:
            stats = waka_data("stats/all_time", token)
            if stats.get("is_up_to_date") is False:
                sleep(4)
                stats = waka_data("stats/all_time", token)
            y = waka_breakdown(body, y, stats)
        except (URLError, TimeoutError, ValueError, KeyError) as error:
            print(f"::warning::WakaTime breakdown unavailable ({type(error).__name__}).")
            body.append(label(16, y, "All-time breakdown currently unavailable.", "meta"))
    save("metrics-wakatime.svg", title, body, y+20)


def waka_breakdown(body, y, stats):
    if stats.get("is_up_to_date") is False:
        body.append(label(16, y, "Breakdown updating at WakaTime", "meta"))
        y += 24
    for key, caption in (("human_readable_daily_average_including_other_language", "Daily average"),
                         ("ai_prompt_events_total", "AI prompts"), ("ai_sessions", "AI sessions"),
                         ("ai_input_tokens", "Input tokens"), ("ai_output_tokens", "Output tokens"),
                         ("ai_model_total_cost", "Estimated AI spend")):
        value = stats.get(key)
        if value is None:
            continue
        if key == "ai_model_total_cost":
            value = f"${value:,.2f}"
        elif isinstance(value, int):
            value = f"{value:,}"
        body.extend([label(16, y, caption, "meta"), label(464, y, value, anchor="end")])
        y += 23
    best = stats.get("best_day") or {}
    if best.get("date"):
        body.append(label(16, y, f"Best day · {best['date']} · {best.get('text', '')}", "meta"))
        y += 24

    sections = []
    if all(stats.get(k) is not None for k in ("ai_additions","ai_deletions","human_additions","human_deletions")):
        changes = {"AI": stats["ai_additions"] + stats["ai_deletions"],
                   "Human": stats["human_additions"] + stats["human_deletions"]}
        sections.append(("Line changes", changes))
    sections.append(("AI model line changes", stats.get("ai_model_line_changes") or {}))
    for heading, values in sections:
        total = sum(values.values())
        if total:
            rows = [(name, value*100/total, f"{value:,}") for name, value in
                    sorted(values.items(), key=lambda item: -item[1])[:5]]
            y = waka_bars(body, y, heading, rows)
    for key in ("languages", "editors", "projects", "categories", "operating_systems"):
        rows = [(row["name"], row["percent"], f"{row['percent']:.0f}%")
                for row in (stats.get(key) or [])[:5]]
        if rows:
            y = waka_bars(body, y, key.replace("_", " ").capitalize(), rows)
    return y


def waka_bars(body, y, heading, rows):
    y += 12
    body.append(label(16, y, heading, "name"))
    for name, percent, display in rows:
        y += 22
        width = max(0, min(100, percent)) * 1.45
        body.extend([label(16, y, clipped(name, 24)),
                     f'<rect class="bar" x="240" y="{y-10}" width="{width:.1f}" height="8" rx="4"/>',
                     label(464, y, display, "meta", "end")])
    return y+24


owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "wikiepeidia")
pages = json.loads(subprocess.check_output(
    ["gh", "api", "--paginate", "--slurp", f"users/{owner}/events/public?per_page=100"]))
events = {event["id"]: event for page in pages for event in page}
render_activity(events.values(), public_contributions(owner))
render_wakatime()
