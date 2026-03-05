"""
OpenOwl Web Tools
━━━━━━━━━━━━━━━━
Free tools for web search and weather.
No API keys needed for most features.

Tools:
  • web_search     — DuckDuckGo (completely free, no key)
  • get_weather    — Open-Meteo (free, no key)
  • get_news       — RSS feeds (free)
  • scrape_url     — Read any webpage
"""
import logging
from typing import Optional
from datetime import datetime

import httpx
from langchain_core.tools import tool

logger = logging.getLogger(__name__)


# ── WEB SEARCH (DuckDuckGo — completely free, no API key) ────────────────────

@tool
def web_search(query: str, max_results: int = 5) -> str:
    """
    Search the web using DuckDuckGo (free, no API key needed).
    Returns top results with titles, URLs, and snippets.
    """
    try:
        import httpx

        # DuckDuckGo Instant Answer API
        url = "https://api.duckduckgo.com/"
        params = {
            "q": query,
            "format": "json",
            "no_html": "1",
            "skip_disambig": "1",
        }

        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, params=params)
            data = resp.json()

        results = []

        # Abstract (direct answer)
        if data.get("AbstractText"):
            results.append(
                f"📌 *{data.get('Heading', 'Answer')}*\n"
                f"{data['AbstractText'][:400]}\n"
                f"Source: {data.get('AbstractURL', '')}"
            )

        # Related topics
        topics = data.get("RelatedTopics", [])[:max_results]
        for topic in topics:
            if isinstance(topic, dict) and topic.get("Text"):
                text = topic["Text"][:200]
                url_t = topic.get("FirstURL", "")
                results.append(f"• {text}\n  {url_t}")

        if not results:
            return _fallback_search(query)

        header = f"🔍 *Search: {query}*\n\n"
        return header + "\n\n".join(results[:max_results])

    except Exception as e:
        logger.warning(f"DuckDuckGo search failed: {e}, trying fallback")
        return _fallback_search(query)


def _fallback_search(query: str) -> str:
    """Fallback: use Brave Search free API or SerpAPI if configured."""
    from config import settings

    # Try Brave Search (free tier: 2000 req/month)
    brave_key = getattr(settings, "brave_search_api_key", "")
    if brave_key:
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": 5},
                    headers={"Accept": "application/json",
                             "X-Subscription-Token": brave_key},
                )
                data = resp.json()

            results = data.get("web", {}).get("results", [])
            lines = [f"🔍 *Search: {query}*\n"]
            for r in results[:5]:
                lines.append(f"• *{r.get('title', '')}*\n  {r.get('description', '')[:150]}\n  {r.get('url', '')}")
            return "\n\n".join(lines)
        except Exception as e:
            logger.warning(f"Brave search failed: {e}")

    return f"🔍 Could not search for '{query}'. Please check your internet connection."


# ── WEATHER (Open-Meteo — completely free, no API key) ───────────────────────

@tool
def get_weather(city: str, country_code: str = "IN") -> str:
    """
    Get current weather and today's forecast for any city.
    Uses Open-Meteo (free, no API key required).
    """
    try:
        # Step 1: Geocode city name → coordinates
        with httpx.Client(timeout=10.0) as client:
            geo = client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1, "language": "en", "format": "json"},
            ).json()

        results = geo.get("results", [])
        if not results:
            return f"❌ City '{city}' not found."

        r      = results[0]
        lat    = r["latitude"]
        lon    = r["longitude"]
        name   = r["name"]
        region = r.get("admin1", "")
        country = r.get("country", "")

        # Step 2: Get weather data
        with httpx.Client(timeout=10.0) as client:
            weather = client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": [
                        "temperature_2m", "apparent_temperature",
                        "weather_code", "wind_speed_10m",
                        "relative_humidity_2m", "precipitation",
                    ],
                    "daily": [
                        "temperature_2m_max", "temperature_2m_min",
                        "precipitation_sum", "weather_code",
                    ],
                    "timezone": "auto",
                    "forecast_days": 3,
                },
            ).json()

        current = weather.get("current", {})
        daily   = weather.get("daily", {})

        temp        = current.get("temperature_2m", "?")
        feels_like  = current.get("apparent_temperature", "?")
        humidity    = current.get("relative_humidity_2m", "?")
        wind        = current.get("wind_speed_10m", "?")
        code        = current.get("weather_code", 0)
        precip      = current.get("precipitation", 0)

        condition = _weather_code_to_text(code)
        emoji     = _weather_code_to_emoji(code)

        location_str = f"{name}, {region}, {country}".strip(", ")
        now_str = datetime.now().strftime("%I:%M %p")

        lines = [
            f"{emoji} *Weather in {location_str}*",
            f"_As of {now_str}_\n",
            f"🌡️ *{temp}°C* (feels like {feels_like}°C)",
            f"☁️ {condition}",
            f"💧 Humidity: {humidity}%",
            f"💨 Wind: {wind} km/h",
        ]

        if precip > 0:
            lines.append(f"🌧️ Precipitation: {precip} mm")

        # 3-day forecast
        if daily.get("time"):
            lines.append("\n📅 *3-day forecast:*")
            for i in range(min(3, len(daily["time"]))):
                date_str = daily["time"][i]
                t_max = daily["temperature_2m_max"][i]
                t_min = daily["temperature_2m_min"][i]
                d_code = daily["weather_code"][i]
                d_emoji = _weather_code_to_emoji(d_code)
                d_label = datetime.fromisoformat(date_str).strftime("%a %d %b")
                lines.append(f"  {d_emoji} {d_label}: {t_min}° – {t_max}°C")

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"Weather fetch failed: {e}")
        return f"❌ Could not get weather for {city}: {e}"


def _weather_code_to_text(code: int) -> str:
    mapping = {
        0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
        45: "Foggy", 48: "Icy fog",
        51: "Light drizzle", 53: "Moderate drizzle", 55: "Heavy drizzle",
        61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
        71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
        80: "Slight showers", 81: "Moderate showers", 82: "Violent showers",
        95: "Thunderstorm", 96: "Thunderstorm with hail",
    }
    return mapping.get(code, "Unknown")


def _weather_code_to_emoji(code: int) -> str:
    if code == 0: return "☀️"
    if code in [1, 2]: return "🌤️"
    if code == 3: return "☁️"
    if code in [45, 48]: return "🌫️"
    if code in range(51, 68): return "🌧️"
    if code in range(71, 78): return "❄️"
    if code in range(80, 83): return "🌦️"
    if code in range(95, 100): return "⛈️"
    return "🌡️"


# ── SCRAPE URL ────────────────────────────────────────────────────────────────

@tool
def scrape_url(url: str, max_chars: int = 1000) -> str:
    """
    Fetch and extract text content from any webpage.
    Useful for reading articles, product pages, etc.
    """
    try:
        with httpx.Client(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; OpenOwl/1.0)"},
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            html = resp.text

        # Basic HTML → text extraction (no extra deps)
        import re
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        if len(text) > max_chars:
            text = text[:max_chars] + "..."

        return f"🌐 Content from {url}:\n\n{text}"

    except Exception as e:
        return f"❌ Could not fetch {url}: {e}"


# ── NEWS ──────────────────────────────────────────────────────────────────────

@tool
def get_top_news(category: str = "general", country: str = "in") -> str:
    """
    Get top news headlines.
    Category: general | technology | business | sports | health | science
    Uses RSS feeds (completely free, no API key).
    """
    rss_feeds = {
        "general":    "https://feeds.feedburner.com/ndtvnews-top-stories",
        "technology": "https://feeds.feedburner.com/gadgets360-latest",
        "business":   "https://feeds.feedburner.com/ndtvprofit-latest",
        "sports":     "https://sports.ndtv.com/rss/all",
        "world":      "https://feeds.bbci.co.uk/news/world/rss.xml",
        "science":    "https://www.nasa.gov/rss/dyn/breaking_news.rss",
    }

    feed_url = rss_feeds.get(category.lower(), rss_feeds["general"])

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                feed_url,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            content = resp.text

        # Parse RSS
        import re
        items = re.findall(r"<item>(.*?)</item>", content, re.DOTALL)[:6]

        if not items:
            return f"📰 No news found for category: {category}"

        headlines = []
        for item in items:
            title_m = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>", item)
            if not title_m:
                title_m = re.search(r"<title>(.*?)</title>", item)
            title = title_m.group(1).strip() if title_m else "No title"
            headlines.append(f"• {title}")

        cat_label = category.title()
        return f"📰 *Top {cat_label} News:*\n\n" + "\n".join(headlines)

    except Exception as e:
        return f"❌ Could not fetch news: {e}"


# All web tools for registration
WEB_TOOLS = [
    web_search,
    get_weather,
    scrape_url,
    get_top_news,
]
