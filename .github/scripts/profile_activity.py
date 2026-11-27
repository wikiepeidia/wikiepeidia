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
.surface { fill: #f6f8fa } .track { fill: #dce4ed }
.hero { fill: white; font-size: 28px; font-weight: 700 }
.kicker { fill: #e3eeff; font-weight: 600; letter-spacing: .7px }
.number { font-weight: 700; font-variant-numeric: tabular-nums }
@media (prefers-color-scheme: dark) {
  text { fill: #f0f6fc } .meta { fill: #b2becb }
  .surface { fill: #161b22 } .track { fill: #303b48 }
}
"""


def label(x, y, value, style="", anchor="start"):
    return (f'<text x="{x}" y="{y}" class="{style}" text-anchor="{anchor}">'
            f'{escape(str(value))}</text>')


def save(filename, title, body, height, gradient=("#153f78", "#55328b")):
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="480" height="{height}" '
           f'viewBox="0 0 480 {height}" role="img"><title>{title}</title>',
           f'<style>{STYLE}</style>',
           '<defs><linearGradient id="ink" x2="1" y2="1" gradientUnits="objectBoundingBox">'
           f'<stop stop-color="{gradient[0]}"/><stop offset="1" stop-color="{gradient[1]}"/></linearGradient></defs>',
           f'<rect class="surface" width="480" height="{height}" rx="14"/>', *body, '</svg>']
    Path("images", filename).write_bytes(("\n".join(svg) + "\n").encode())


def banner(caption, value, accent="#aba9ff"):
    return ['<rect width="480" height="86" rx="14" fill="url(#ink)"/>',
            '<path d="M335 85 L403 17 L471 85 M376 85 L443 18 L480 55" '
            f'fill="none" stroke="{accent}" stroke-opacity=".26" stroke-width="3"/>',
            label(18, 28, caption, "kicker"), label(18, 64, value, "hero")]


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
        totalPullRequestReviewContributions
        commits:commitContributionsByRepository(maxRepositories:100){repository{id visibility}}
        issues:issueContributionsByRepository(maxRepositories:100){repository{id visibility}}
        prs:pullRequestContributionsByRepository(maxRepositories:100){repository{id visibility}}
        reviews:pullRequestReviewContributionsByRepository(maxRepositories:100){repository{id visibility}}
        created:repositoryContributions(first:100){nodes{repository{id visibility}} pageInfo{hasNextPage}}
        contributionCalendar { weeks { contributionDays { date contributionCount } } }
      } }
    }"""
    history = {}
    repositories = set()
    reviews = 0
    capped = False
    for year in range(joined.year, today.year + 1):
        start = max(joined, date(year, 1, 1))
        end = min(today, date(year, 12, 31))
        user = graphql_user(owner, query, f"from={start}T00:00:00Z", f"to={end}T23:59:59Z")
        collection = user["contributionsCollection"]
        reviews += collection["totalPullRequestReviewContributions"]
        groups = [collection[key] for key in ("commits", "issues", "prs", "reviews")]
        capped |= any(len(group) == 100 for group in groups) or collection["created"]["pageInfo"]["hasNextPage"]
        for group in [*groups, collection["created"]["nodes"]]:
            repositories.update(item["repository"]["id"] for item in group
                                if item["repository"] and item["repository"]["visibility"] == "PUBLIC")
        for week in collection["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                when = date.fromisoformat(day["date"])
                if start <= when <= end:
                    history[when] = day["contributionCount"]
    return history, reviews, f"{len(repositories)}{'+' if capped else ''}"


def render_rank_streak(history, joined, today, rank, reviews):
    total = run = current = longest = 0
    best_end = None
    for offset in range((today - joined).days + 1):
        day = joined + timedelta(days=offset)
        count = history.get(day, 0)
        total += count
        run = run + 1 if count else 0
        if run > longest:
            longest, best_end = run, day
        if day == today - timedelta(days=1) or (day == today and count):
            current = run
    body = banner("GITHUB README RANK", f"Grade {rank}", "#ffda85")
    body.append(label(18, 116, f"{total:,} contributions since {joined:%b %Y}", "name"))
    body.append(label(18, 140, f"{reviews:,} pull request review contributions since {joined:%Y}", "meta"))
    for index, grade in enumerate(("C", "C+", "B-", "B", "B+", "A-", "A", "A+", "S")):
        x = 18 + index * 50
        active = grade == rank
        fill = 'fill="#864413"' if active else 'class="track"'
        body.append(f'<rect x="{x}" y="157" width="42" height="30" rx="8" {fill}/>')
        body.append(label(x+21, 178, grade, "kicker" if active else "number", "middle"))
    body.append(label(18, 224, "Contribution streak", "heading"))
    for y, caption, days, color in ((257, "Current", current, "#aa5823"), (290, "Longest", longest, "#c1841b")):
        body.append(label(18, y, caption))
        body.append(f'<rect class="track" x="109" y="{y-11}" width="245" height="9" rx="4"/>')
        width = 245 * days / max(longest, 1)
        body.append(f'<rect fill="{color}" x="109" y="{y-11}" width="{width:.1f}" height="9" rx="4"/>')
        body.append(label(462, y, f"{days} day" if days == 1 else f"{days} days", "number", "end"))
    if best_end:
        best_start = best_end - timedelta(days=longest - 1)
        body.append(label(18, 322, f"Best streak  ·  {best_start:%d %b %Y} – {best_end:%d %b %Y}", "meta"))
    save("metrics-activity.svg", f"README rank {rank}, current streak {current} days, best {longest} days", body, 343, ("#502820", "#864413"))


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
    body = banner("WAKATIME", "All-time coding")
    token = os.environ.get("WAKATIME_TOKEN")
    y = 118
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
            body = banner("WAKATIME  ·  ALL TIME", duration(max(lifetime_seconds, observed)) + caption)
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
    metrics = []
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
        metrics.append((caption, value))
    for index, (caption, value) in enumerate(metrics):
        x = 18 + (index % 2) * 226
        top = y + (index // 2) * 57
        body.extend([f'<rect x="{x}" y="{top-12}" width="3" height="35" rx="1.5" fill="#8175d6"/>',
                     label(x+12, top, caption, "meta"), label(x+12, top+23, value, "name number")])
    y += ((len(metrics)+1)//2)*57
    if stats.get("ai_model_total_cost") is not None:
        try:
            with urlopen("https://open.er-api.com/v6/latest/USD", timeout=8) as response:
                rates = json.load(response)
            vnd = stats["ai_model_total_cost"] * rates["rates"]["VND"]
            rate_date = datetime.fromtimestamp(rates["time_last_update_unix"], timezone.utc).date()
            body.append(label(16, y, f"Approx. {vnd:,.0f} VND  ·  USD rate on {rate_date}", "meta"))
            y += 23
        except (URLError, TimeoutError, KeyError, ValueError, TypeError):
            print("::warning::USD/VND rate unavailable.")
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
    palette = {"Languages": "#287fc1", "Editors": "#895cc7", "Projects": "#188576",
               "Categories": "#bc6941", "Operating systems": "#5270ba",
               "Line changes": "#a85492", "AI model line changes": "#765bc7"}
    color = palette[heading]
    for name, percent, display in rows:
        y += 22
        width = max(0, min(100, percent)) * 1.45
        body.extend([label(16, y, clipped(name, 24)),
                     f'<rect class="track" x="240" y="{y-10}" width="145" height="8" rx="4"/>',
                     f'<rect fill="{color}" x="240" y="{y-10}" width="{width:.1f}" height="8" rx="4"/>',
                     label(464, y, display, "meta", "end")])
    return y+24


owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "wikiepeidia")
joined, rank = profile_rank(owner)
today = datetime.now(timezone(timedelta(hours=7))).date()
history, reviews, repo_count = daily_history(owner, joined, today)
render_rank_streak(history, joined, today, rank, reviews)
if os.environ.get("GITHUB_OUTPUT"):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf8") as output:
        print(f"repo_count={repo_count}", file=output)
render_wakatime()
