"""Render two self-contained, theme-aware SVG cards from public GitHub feeds."""

import json
import os
import subprocess
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


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


def save(filename, title, body, height):
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="480" height="{height}" '
           f'viewBox="0 0 480 {height}" role="img"><title>{title}</title>',
           f'<style>{STYLE}</style>', *body, '</svg>']
    Path("images", filename).write_bytes(("\n".join(svg) + "\n").encode())


def clipped(value, limit):
    return value if len(value) <= limit else value[:limit - 1] + "…"


def profile_rank(owner):
    query = """query($login:String!) { user(login:$login) {
      createdAt followers { totalCount } pullRequests { totalCount } issues { totalCount }
      contributionsCollection { totalPullRequestReviewContributions }
    } }"""
    user = graphql_user(owner, query)
    commits = json.loads(subprocess.check_output(
        ["gh", "api", f"search/commits?q=author:{owner}&per_page=1"]))
    if commits.get("incomplete_results"):
        raise RuntimeError("GitHub commit search returned incomplete results")
    repos = json.loads(subprocess.check_output(
        ["gh", "api", "--paginate", "--slurp", f"users/{owner}/repos?type=owner&per_page=100"]))
    stars = sum(repo["stargazers_count"] for page in repos for repo in page if not repo["private"])
    rank = readme_rank(commits["total_count"], user["pullRequests"]["totalCount"],
                      user["issues"]["totalCount"], user["contributionsCollection"]["totalPullRequestReviewContributions"],
                      stars, user["followers"]["totalCount"])
    return date.fromisoformat(user["createdAt"][:10]), rank


def graphql_user(owner, query, *variables):
    command = ["gh", "api", "graphql", "-f", f"query={query}", "-f", f"login={owner}"]
    for variable in variables:
        command.extend(["-f", variable])
    return json.loads(subprocess.check_output(command))["data"]["user"]


def readme_rank(commits, prs, issues, reviews, stars, followers):
    # github-readme-stats/src/calculateRank.js, include_all_commits=true.
    weighted = sum(weight * (1 - 2 ** (-count / median)) for count, median, weight in
                   [(commits, 1000, 2), (prs, 50, 3), (issues, 25, 1), (reviews, 2, 1)])
    weighted += 4 * stars / (stars + 50) + followers / (followers + 10)
    score = 100 * (1 - weighted / 12)
    thresholds = [(1, "S"), (12.5, "A+"), (25, "A"), (37.5, "A-"), (50, "B+"),
                  (62.5, "B"), (75, "B-"), (87.5, "C+"), (100, "C")]
    return next(grade for limit, grade in thresholds if score <= limit)


def daily_history(owner, joined, today):
    query = """query($login:String!, $from:DateTime!, $to:DateTime!) {
      user(login:$login) { contributionsCollection(from:$from, to:$to) {
        contributionCalendar { weeks { contributionDays { date contributionCount } } }
      } }
    }"""
    history = {}
    for year in range(joined.year, today.year + 1):
        start = max(joined, date(year, 1, 1))
        end = min(today, date(year, 12, 31))
        user = graphql_user(owner, query, f"from={start}T00:00:00Z", f"to={end}T23:59:59Z")
        for week in user["contributionsCollection"]["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                when = date.fromisoformat(day["date"])
                if start <= when <= end:
                    history[when] = day["contributionCount"]
    return history


def render_rank_streak(history, joined, today, rank):
    total = run = current = longest = 0
    body = []
    for offset in range((today - joined).days + 1):
        day = joined + timedelta(days=offset)
        count = history.get(day, 0)
        total += count
        run = run + 1 if count else 0
        longest = max(longest, run)
        if day == today - timedelta(days=1) or (day == today and count):
            current = run
    body.extend([label(16, 30, "GitHub README rank", "heading"),
                 label(16, 55, f"{total:,} contributions since {joined:%b %Y}", "meta")])
    for index, grade in enumerate(("C", "C+", "B-", "B", "B+", "A-", "A", "A+", "S")):
        x = 16 + index * 50
        if grade == rank:
            body.append(f'<rect x="{x}" y="68" width="44" height="32" rx="8" fill="#166534"/>')
            body.append(f'<text x="{x+22}" y="90" text-anchor="middle" style="fill:white;font-weight:700">{grade}</text>')
        else:
            body.append(label(x+22, 90, grade, "meta", "middle"))
    body.append(label(16, 138, "Contribution streak", "heading"))
    for y, caption, days in ((171, "Current", current), (202, "Longest", longest)):
        body.append(label(16, y, caption))
        body.append(f'<rect x="95" y="{y-12}" width="272" height="10" rx="5" fill="#888" opacity=".22"/>')
        width = 272 * days / max(longest, 1)
        body.append(f'<rect class="bar" x="95" y="{y-12}" width="{width:.1f}" height="10" rx="5"/>')
        body.append(label(464, y, f"{days} day" if days == 1 else f"{days} days", "name", "end"))
    save("metrics-activity.svg", f"README rank {rank}, current streak {current} days, best {longest} days", body, 222)


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
joined, rank = profile_rank(owner)
today = datetime.now(timezone(timedelta(hours=7))).date()
history = daily_history(owner, joined, today)
render_rank_streak(history, joined, today, rank)
render_wakatime()
