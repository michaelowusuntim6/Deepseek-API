"""
weather.py — DeepSeek CLI extension: get current weather via Open-Meteo.

Drop this file into ~/.deepseek-tui/extensions/ and restart (or /reload).

Uses the free Open-Meteo API — no API key required.
Only stdlib: urllib, json.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from deepseek import tool


@tool
def get_weather(city: str, unit: str = "celsius") -> str:
    """Get the current temperature for a city using the free Open-Meteo API.

    Args:
        city: Name of the city (e.g. "Tokyo", "Paris").
        unit: Temperature unit — "celsius" (default) or "fahrenheit".
    """
    unit = unit.lower().strip()
    if unit not in ("celsius", "fahrenheit"):
        unit = "celsius"

    temperature_unit = "celsius" if unit == "celsius" else "fahrenheit"
    degree_symbol = "°C" if unit == "celsius" else "°F"

    try:
        # Step 1: Geocode the city name → (lat, lon)
        geo_params = urllib.parse.urlencode({
            "name": city,
            "count": 1,
            "language": "en",
            "format": "json",
        })
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?{geo_params}"
        req = urllib.request.Request(geo_url, headers={"User-Agent": "DeepSeek-CLI-ext/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            geo_data = json.loads(resp.read().decode("utf-8"))

        results = geo_data.get("results")
        if not results:
            return f"Error: city '{city}' not found by geocoding API."

        lat = results[0]["latitude"]
        lon = results[0]["longitude"]
        resolved_name = results[0].get("name", city)
        country = results[0].get("country", "")

        # Step 2: Fetch current temperature
        wx_params = urllib.parse.urlencode({
            "latitude": lat,
            "longitude": lon,
            "current_weather": "true",
            "temperature_unit": temperature_unit,
            "windspeed_unit": "kmh",
            "timezone": "auto",
        })
        wx_url = f"https://api.open-meteo.com/v1/forecast?{wx_params}"
        req = urllib.request.Request(wx_url, headers={"User-Agent": "DeepSeek-CLI-ext/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            wx_data = json.loads(resp.read().decode("utf-8"))

        current = wx_data.get("current_weather", {})
        temp = current.get("temperature")
        windspeed = current.get("windspeed")
        weathercode = current.get("weathercode")

        if temp is None:
            return f"Error: weather data unavailable for '{city}'."

        location_str = f"{resolved_name}, {country}".rstrip(", ")
        result = f"{temp}{degree_symbol} in {location_str}"
        if windspeed is not None:
            result += f" · wind {windspeed} km/h"
        if weathercode is not None:
            result += f" · WMO code {weathercode}"
        return result

    except urllib.error.HTTPError as e:
        return f"Error: HTTP {e.code} {e.reason} from weather API."
    except urllib.error.URLError as e:
        return f"Error: URL error from weather API: {e.reason}"
    except TimeoutError:
        return "Error: weather API request timed out (10s)."
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


def register():
    """Return the list of tools provided by this extension."""
    return [get_weather]
