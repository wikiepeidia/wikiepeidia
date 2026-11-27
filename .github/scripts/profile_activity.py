"""Render two self-contained, theme-aware SVG cards from public GitHub feeds."""

import json
import os
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
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
             + selections + " contributionCalendar { weeks { contributionDays { date weekday contributionCount color } } } } } }")
    result = json.loads(subprocess.check_output(
        ["gh", "api", "graphql", "-f", f"query={query}", "-f", f"login={owner}"]))
    counts = defaultdict(Counter)
    collection = result["data"]["user"]["contributionsCollection"]
    weeks = collection.pop("contributionCalendar")["weeks"]
    for kind, rows in collection.items():
        for row in rows:
            if not row["repository"]["isPrivate"]:
                counts[row["repository"]["nameWithOwner"]][kind] = row["contributions"]["totalCount"]
    return counts, weeks


def render_activity(events, contributions, weeks, profile_repo):
    recent = Counter()
    created_repos = set()
    for event in sorted(events, key=lambda item: item["created_at"], reverse=True):
        repo = event["repo"]["name"]
        kind = event["type"]
        recent[repo] += 1
        if kind == "CreateEvent" and event["payload"].get("ref_type") == "repository":
            created_repos.add(repo)

    weeks = weeks[-26:]
    total = sum(day["contributionCount"] for week in weeks for day in week["contributionDays"])
    body = [label(16, 32, "Daily contributions", "heading"),
            label(16, 56, f"{total:,} contributions · last {len(weeks)} calendar weeks", "meta")]
    previous_month = None
    for column, week in enumerate(weeks):
        days = week["contributionDays"]
        x = 48 + column * 16
        month = datetime.fromisoformat(days[0]["date"]).strftime("%b")
        if month != previous_month and column < len(weeks)-1:
            body.append(label(x, 78, month, "meta"))
        previous_month = month
        for day in days:
            y = 84 + day["weekday"] * 15
            body.append(f'<rect x="{x}" y="{y}" width="13" height="13" rx="2" fill="{day["color"]}">'
                        f'<title>{day["date"]}: {day["contributionCount"]} contributions</title></rect>')
    for row, weekday in ((1, "M"), (3, "W"), (5, "F")):
        body.append(label(20, 95 + row * 15, weekday, "meta"))

    body.append(label(16, 218, "Profile and recent repositories", "heading"))
    body.append(label(16, 243, "Past year’s contributions · recent public event counts", "meta"))
    y = 280
    # First appearance records the newest event for each distinct repository.
    repos = [profile_repo, *[repo for repo in recent if repo.casefold() != profile_repo.casefold()][:5]]
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
    save("metrics-activity.svg", "Daily contributions and recent activity", body, y-8)


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


def waka_optional(endpoint, token, default):
    try:
        return waka_data(endpoint, token)
    except (URLError, TimeoutError, ValueError, KeyError) as error:
        print(f"::warning::WakaTime {endpoint.split('?')[0]} unavailable ({type(error).__name__}).")
        return default


def summary_stats(days):
    """Combine daily totals and weight chart percentages by tracked seconds."""
    stats = {}
    models = Counter()
    sections = defaultdict(Counter)
    active = []
    for day in days:
        totals = day["grand_total"]
        seconds = totals["total_seconds"]
        if seconds:
            active.append((seconds, day["range"]["date"]))
        for key in ("ai_additions", "ai_deletions", "human_additions", "human_deletions",
                    "ai_sessions", "ai_prompt_events_total", "ai_input_tokens",
                    "ai_output_tokens", "ai_model_total_cost"):
            if totals.get(key) is not None:
                stats[key] = stats.get(key, 0) + totals[key]
        models.update(totals.get("ai_model_line_changes") or {})
        for section in ("languages", "editors", "projects", "categories", "operating_systems"):
            for row in day.get(section) or []:
                sections[section][row["name"]] += row["total_seconds"]
    stats["ai_model_line_changes"] = models
    for key, counts in sections.items():
        total = sum(counts.values())
        if total:
            stats[key] = [{"name": name, "percent": seconds * 100 / total}
                          for name, seconds in counts.most_common()]
    if active:
        seconds, date = max(active)
        stats["best_day"] = {"date": date, "text": duration(seconds)}
        stats["human_readable_daily_average_including_other_language"] = duration(
            sum(seconds for seconds, _ in active) / len(active))
    return stats


def render_wakatime():
    title = "WakaTime · all time"
    body = [label(16, 32, title, "heading")]
    token = os.environ.get("WAKATIME_TOKEN")
    y = 68
    if not token:
        body.append(label(16, y, "Waiting for WakaTime access.", "meta"))
    else:
        lifetime = waka_optional("all_time_since_today", token, {})
        stats = waka_optional("stats/all_time", token, {})
        today = datetime.now(timezone(timedelta(hours=7))).date()
        start = today - timedelta(days=6)
        days = waka_optional(f"summaries?start={start}&end={today}&timezone=Asia%2FHo_Chi_Minh", token, [])
        observed = sum(day["grand_total"]["total_seconds"] for day in days)
        aggregate = stats.get("total_seconds_including_other_language") or stats.get("total_seconds") or 0
        lifetime_seconds = max(lifetime.get("total_seconds") or 0, aggregate)
        if lifetime_seconds or observed:
            lagging = observed > lifetime_seconds
            caption = " or more tracked" if lagging else " tracked since joining"
            body.append(label(16, y, duration(max(lifetime_seconds, observed)) + caption, "name"))
            y += 28
        if observed and (aggregate < observed or not stats.get("languages")):
            # A new account can have daily data before lifetime charts are computed.
            stats = summary_stats(days)
            dates = sorted(day["range"]["date"] for day in days if day["grand_total"]["total_seconds"])
            body.append(label(16, y, f"Recorded breakdown · {dates[0]} to {dates[-1]}", "meta"))
            y += 26
        elif not aggregate:
            body.append(label(16, y, "Waiting for recorded coding activity.", "meta"))
            stats = {}
            y += 24
        y = waka_breakdown(body, y, stats)
    save("metrics-wakatime.svg", title, body, y+20)


def waka_breakdown(body, y, stats):
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
contributions, weeks = public_contributions(owner)
render_activity(events.values(), contributions, weeks, f"{owner}/{owner}")
render_wakatime()
