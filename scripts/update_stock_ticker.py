"""Render the profile's Stock Ticker from the public GitHub contribution calendar."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from html import escape
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps


DAY = timedelta(days=1)
WINDOW = 26 * 7
SCALE = 2
OWNER = "RafaelADSdev"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "stock-ticker"
ZONA = "America/Sao_Paulo"
MONTHS = ("JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ")
FONT_DIR = Path(os.environ.get("STOCK_TICKER_FONT_DIR", "/usr/share/fonts/truetype/dejavu"))

FOREGROUND = "#EBF3FA"
MUTED = "#A0AAB5"
GREEN = "#43BF5A"
RED = "#F95652"
BORDER = "#252D37"
DIVIDER = "#262E37"
REGULAR = "DejaVuSans.ttf"
HEAVY = "DejaVuSans-Bold.ttf"
MONO = "DejaVuSansMono.ttf"
MONO_BOLD = "DejaVuSansMono-Bold.ttf"

PROJECTS = (
    ("Vercuno", "Vercuno", "TS"),
    ("Prumo", "prumo", "TS"),
    ("Bloco", "notionREA", "TS"),
    ("Zelo", "Zelo", "JS"),
)
USER_QUERY = """
query StockTicker($owner: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $owner) {
    avatarUrl(size: 192)
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
"""
REPO_QUERY = """
  repo{index}: repository(owner: $owner, name: "{repo}") {{
    stargazerCount
    defaultBranchRef {{ target {{ ... on Commit {{ committedDate }} }} }}
  }}
"""
QUERY = USER_QUERY + "".join(
    REPO_QUERY.format(index=index, repo=repo)
    for index, (_, repo, _) in enumerate(PROJECTS)
) + "}"


@dataclass(frozen=True)
class Project:
    name: str
    symbol: str
    stars: int | None
    active: bool


@dataclass(frozen=True)
class Activity:
    day: date
    counts: dict[date, int]
    projects: tuple[Project, ...]

    @property
    def start(self):
        return self.day - (WINDOW - 1) * DAY

    @property
    def year_start(self):
        return self.day.replace(month=1, day=1)

    def calendar(self):
        return [self.counts[self.start + index * DAY] for index in range(WINDOW)]

    def moving_total(self, day):
        return sum(self.counts[day - offset * DAY] for offset in range(30))

    def weekly_candles(self):
        for index in range(26):
            first = self.start + index * 7 * DAY
            values = [self.moving_total(first + offset * DAY) for offset in range(-1, 7)]
            volume = sum(self.counts[first + offset * DAY] for offset in range(7))
            yield values[0], max(values), min(values), values[-1], volume

    def summary(self):
        calendar = self.calendar()
        longest = run = 0
        for count in calendar:
            run = run + 1 if count else 0
            longest = max(longest, run)
        latest_days = calendar if calendar[-1] else calendar[:-1]
        current = 0
        for count in reversed(latest_days):
            if count == 0:
                break
            current += 1
        year = sum(value for day, value in self.counts.items() if self.year_start <= day <= self.day)
        return current, longest, year

    def latest(self):
        return self.moving_total(self.day), self.moving_total(self.day - 28 * DAY)


def get_activity(now, token):
    day = now.date()
    first_day = min(day.replace(month=1, day=1), day - (WINDOW + 29) * DAY)
    variables = {
        "owner": OWNER,
        "from": datetime.combine(first_day, time.min, timezone.utc).isoformat(),
        "to": now.astimezone(timezone.utc).isoformat(),
    }
    request = Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": QUERY, "variables": variables}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "rafael-stock-ticker",
        },
    )
    with urlopen(request, timeout=30) as response:
        payload = json.load(response)
    data = payload.get("data") or {}
    user = data.get("user")
    if not user or not user.get("contributionsCollection"):
        messages = [error["message"] for error in payload.get("errors", [])]
        raise RuntimeError("O GitHub não retornou o calendário de contribuições. " + " ".join(messages))

    calendar = user["contributionsCollection"]["contributionCalendar"]
    counts = {
        date.fromisoformat(entry["date"]): entry["contributionCount"]
        for week in calendar["weeks"]
        for entry in week["contributionDays"]
    }
    missing = set(first_day + offset * DAY for offset in range((day - first_day).days)) - counts.keys()
    if missing:
        raise RuntimeError(f"O calendário chegou incompleto. Primeira data ausente: {min(missing)}")
    counts.setdefault(day, 0)

    projects = []
    for index, (label, _, symbol) in enumerate(PROJECTS):
        repo = data.get(f"repo{index}")
        head = (repo.get("defaultBranchRef") or {}).get("target") if repo else None
        committed_at = (head or {}).get("committedDate")
        recent = False
        if committed_at:
            commit_time = datetime.fromisoformat(committed_at)
            recent = now - 30 * DAY <= commit_time <= now
        projects.append(Project(label, symbol, repo["stargazerCount"] if repo else None, recent))

    avatar_url = user["avatarUrl"]
    url = urlsplit(avatar_url)
    if url.scheme != "https" or url.hostname != "avatars.githubusercontent.com":
        raise RuntimeError("O GitHub retornou um endereço de avatar inesperado.")
    with urlopen(avatar_url, timeout=15) as response:
        encoded_avatar = response.read()
    with Image.open(BytesIO(encoded_avatar)) as profile:
        avatar = profile.convert("RGB")
    return Activity(day, counts, tuple(projects)), avatar


@lru_cache
def font(name, size):
    return ImageFont.truetype(str(FONT_DIR / name), size * SCALE)


class Canvas:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.image = Image.new("RGBA", (width * SCALE, height * SCALE))
        self.draw = ImageDraw.Draw(self.image)

    @staticmethod
    def grid(*points):
        return tuple(round(point * SCALE) for point in points)

    def rect(self, points, fill, radius=0, outline=None):
        bounds = self.grid(*points)
        if radius > 0:
            self.draw.rounded_rectangle(bounds, radius=radius * SCALE, fill=fill, outline=outline, width=SCALE)
        else:
            self.draw.rectangle(bounds, fill=fill, outline=outline, width=SCALE)

    def line(self, points, color, weight=1):
        self.draw.line(self.grid(*points), fill=color, width=round(weight * SCALE))

    def text(self, x, y, value, face, size, color=FOREGROUND, anchor="lt"):
        self.draw.text(self.grid(x, y), value, font=font(face, size), fill=color, anchor=anchor)

    def circle(self, points, fill):
        self.draw.ellipse(self.grid(*points), fill=fill)

    def star(self, x, y, size, color=FOREGROUND):
        center_x, center_y = x + size / 2, y + size / 2
        points = []
        for index in range(10):
            angle = index * math.pi / 5 - math.pi / 2
            radius = size * (0.5 if index % 2 == 0 else 0.22)
            points.append(self.grid(center_x + radius * math.cos(angle), center_y + radius * math.sin(angle)))
        self.draw.polygon(points, fill=color)

    def avatar(self, x, y, portrait, size, rim=3):
        self.circle((x, y, x + size, y + size), GREEN)
        diameter = (size - 2 * rim) * SCALE
        face = ImageOps.fit(portrait, (diameter, diameter), method=Image.Resampling.LANCZOS)
        mask = Image.new("L", (diameter, diameter))
        ImageDraw.Draw(mask).ellipse((0, 0, diameter - 1, diameter - 1), fill=255)
        self.image.paste(face, self.grid(x + rim, y + rim), mask)

    def save(self, path):
        self.image.save(path, "WEBP", quality=91, method=5)


def asset_name(section, mobile):
    if section == "painel":
        return "mobile.webp" if mobile else "painel.webp"
    return f"{section}-mobile.webp" if mobile else f"{section}.webp"


def card(width, height, title, mobile):
    panel = Canvas(width, height)
    panel.rect((1, 1, width - 1, height - 1), "#0F141A", radius=14, outline=BORDER)
    inset = 27 if mobile else 39
    header = 59 if mobile else 71
    panel.text(inset, header / 2 + 5, "▲ " + title, MONO_BOLD, 17 if mobile else 15, GREEN, "lm")
    panel.line((1, header, width - 1, header), DIVIDER)
    return panel, inset, header


def tech_mark(panel, x, y, symbol, size):
    tile, ink = ("#3278BE", "#F2F7FF") if symbol == "TS" else ("#F2DE5E", "#18202A")
    panel.rect((x, y, x + size, y + size), tile, radius=3)
    panel.text(x + size / 2, y + size / 2, symbol, MONO_BOLD, round(size * 0.57), ink, "mm")


def ticker(activity, portrait, mobile, directory):
    width, height = (640, 505) if mobile else (1000, 552)
    panel = Canvas(width, height)
    margin = 20
    right = width - margin
    start, end = ImageColor.getrgb("#16201D"), ImageColor.getrgb("#0F1419")
    for y in range(height * SCALE):
        offset = y / (height * SCALE - 1)
        color = tuple(round(a + (b - a) * offset) for a, b in zip(start, end))
        panel.draw.line((0, y, width * SCALE, y), fill=color)

    profile_size = 50 if mobile else 58
    text_x = margin + profile_size + 14
    panel.avatar(margin, 22, portrait, profile_size)
    panel.text(text_x, 24, "$RAFAE", HEAVY, 37 if mobile else 38)
    if not mobile:
        word_width = panel.draw.textlength("$RAFAE", font=font(HEAVY, 38)) / SCALE
        pill_left = text_x + math.ceil(word_width) + 16
        panel.rect((pill_left, 27, pill_left + 209, 57), "#172A37", radius=15, outline="#294C66")
        tech_mark(panel, pill_left + 13, 35, "TS", 14)
        panel.text(pill_left + 33, 37, "SETOR · TYPESCRIPT", MONO_BOLD, 14, "#77B4E9")
    banner_line = "Rafael Arcanjo · GitHub" if mobile else "Rafael Arcanjo · GitHub · contribuições nos últimos 30 dias"
    panel.text(text_x, 68, banner_line, REGULAR, 17, MUTED)

    current, previous = activity.latest()
    change = current - previous
    trend = "▲" if change > 0 else "▼" if change < 0 else "•"
    color = GREEN if change > 0 else RED if change < 0 else MUTED
    delta = (f"{abs(change) / previous:.1%}".replace(".", ",") if previous else f"{abs(change)}")
    price = "$" + f"{current * 0.30:.2f}"
    panel.text(right, 21, price, HEAVY, 41, anchor="rt")
    panel.text(right, 69, f"{trend} {delta} · 4 SEMANAS", MONO_BOLD, 15, color, "rt")

    candles = list(activity.weekly_candles())
    plot_top, plot_bottom = 111, (350 if mobile else 388)
    volume_end = 418 if mobile else 467
    peak_volume = max(row[4] for row in candles)
    roof = max(20, math.ceil(max(row[1] for row in candles) / 20) * 20)
    slot = (right - margin) / len(candles)
    bar_width = slot * 0.64

    def spot(count):
        return plot_bottom - count * (plot_bottom - plot_top) / roof

    for line_no in range(5):
        yy = plot_top + line_no * (plot_bottom - plot_top) / 4
        panel.line((margin, yy, right, yy), "#273237")
    for index, (opened, high, low, closed, volume) in enumerate(candles):
        xx = margin + (index + 0.5) * slot
        paint = GREEN if closed > opened else RED if closed < opened else "#69767C"
        panel.line((xx, spot(high), xx, spot(low)), paint, 1.5)
        top, bottom = sorted((spot(opened), spot(closed)))
        panel.rect((xx - bar_width / 2, top, xx + bar_width / 2, max(top + 1.5, bottom)), paint, radius=1)
        if volume:
            cap = volume_end - (volume / peak_volume) * (volume_end - plot_bottom - 13)
            panel.rect((xx - bar_width / 2, cap, xx + bar_width / 2, volume_end), "#3E4650", radius=1)

    panel.text(margin, volume_end + 12, "VOLUME SEMANAL · 26 SEMANAS", MONO, 14, MUTED)
    collected = activity.day
    stamp = f"{collected.day:02d} {MONTHS[collected.month - 1]} {collected.year}"
    panel.text(right, volume_end + 12, stamp, MONO, 14, MUTED, "rt")
    tape_top = height - 50
    panel.rect((margin, tape_top, right, height - 10), "#171F27", radius=3)
    unit = (right - margin) / 4
    for index, project in enumerate(activity.projects):
        x = margin + index * unit + 12
        tech_mark(panel, x, tape_top + 11, project.symbol, 18)
        panel.text(x + 27, tape_top + 14, project.name.upper(), MONO_BOLD, 16)
        if not mobile:
            panel.star(x + 124, tape_top + 13, 14)
            stars = "–" if project.stars is None else str(project.stars)
            panel.text(x + 143, tape_top + 14, stars, MONO_BOLD, 15)
            status = "▲" if project.active else "■"
            panel.text(x + 177, tape_top + 14, status, MONO_BOLD, 15, GREEN if project.active else MUTED)
    panel.save(directory / asset_name("painel", mobile))


def days_label(count):
    return f"{count} {'dia' if count == 1 else 'dias'}"


def volume(activity, mobile, directory):
    width, height = (640, 365) if mobile else (1000, 300)
    panel, inset, heading = card(width, height, "VOLUME · 26 SEMANAS", mobile)
    y_start = heading + 25
    pitch = 22 if mobile else 18.2
    square = 18 if mobile else 15.2
    palette = ("#1A212A", "#11492D", "#13773C", "#22A144", "#41CB55")
    levels = (0, 1, 3, 6, 10)
    for index, count in enumerate(activity.calendar()):
        column, row = divmod(index, 7)
        color = palette[sum(count >= min_count for min_count in levels) - 1]
        xx = inset + column * pitch
        yy = y_start + row * pitch
        panel.rect((xx, yy, xx + square, yy + square), color, radius=3)

    current, longest, total = activity.summary()
    stats = (
        ("SEQUÊNCIA ATUAL", days_label(current)),
        ("MELHOR SEQUÊNCIA", days_label(longest)),
        ("VOLUME NO ANO", str(total)),
    )
    for index, (label, value) in enumerate(stats):
        xx = (inset + index * 199) if mobile else 592
        yy = 272 if mobile else y_start + 64 * index
        panel.text(xx, yy, label, MONO, 15, MUTED)
        panel.text(xx, yy + 23, value, HEAVY, 30)
    panel.save(directory / asset_name("volume", mobile))


def update_alt(readme, name, description):
    image = re.compile(rf'<img\b[^>]*\bsrc="(?:\./)?assets/stock-ticker/{name}\.webp"[^>]*>')
    escaped = escape(description, quote=True)
    return image.sub(
        lambda match: re.sub(r'\balt="[^"]*"', lambda _: f'alt="{escaped}"', match[0], count=1),
        readme,
        count=1,
    )


def user_facing_readme(readme, activity):
    current, previous = activity.latest()
    price = f"{current * 0.30:.2f}".replace(".", ",")
    change_text = f"Há quatro semanas, o total em 30 dias era de {previous}."
    hero_alt = (
        f"$RAFAE. Gráfico de velas das últimas 26 semanas. Cotação visual de US$ {price} "
        f"para {current} contribuições nos últimos 30 dias. {change_text} "
        f"Dados até {activity.day:%d/%m/%Y}."
    )
    current_streak, longest, year = activity.summary()
    heatmap_alt = (
        "Mapa de contribuições das últimas 26 semanas. "
        f"Sequência atual de {days_label(current_streak)}, melhor sequência de {days_label(longest)} no período "
        f"e {year} contribuições no ano. Dados até {activity.day:%d/%m/%Y}."
    )
    return update_alt(update_alt(readme, "painel", hero_alt), "volume", heatmap_alt)


def render(activity, avatar, directory):
    for mobile in (False, True):
        ticker(activity, avatar, mobile, directory)
        volume(activity, mobile, directory)


def main():
    activity, avatar = get_activity(datetime.now(ZoneInfo(ZONA)), os.environ["GITHUB_TOKEN"])
    readme = ROOT / "README.md"
    updated_readme = user_facing_readme(readme.read_text(encoding="utf-8"), activity)
    with TemporaryDirectory(dir=OUT.parent) as temp:
        directory = Path(temp)
        render(activity, avatar, directory)
        for asset in directory.iterdir():
            asset.replace(OUT / asset.name)
    readme.write_text(updated_readme, encoding="utf-8")
    print(f"Dados até {activity.day:%d/%m/%Y}. Contribuições em 30 dias: {activity.latest()[0]}.")


if __name__ == "__main__":
    main()
