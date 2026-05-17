#!/usr/bin/env python3
"""Update the current weekly training file from Intervals.icu and OpenAI."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


LISBON_TZ = ZoneInfo("Europe/Lisbon")
REPO_ROOT = Path(__file__).resolve().parents[1]
INTERVALS_BASE_URL = "https://intervals.icu/api/v1"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
RUSSIAN_MONTHS = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}
RUSSIAN_WEEKDAYS = {
    0: "понедельник",
    1: "вторник",
    2: "среда",
    3: "четверг",
    4: "пятница",
    5: "суббота",
    6: "воскресенье",
}


def main() -> int:
    args = parse_args()
    now = datetime.now(LISBON_TZ)
    target_date = parse_target_date(args.date) if args.date else now.date()
    force_run = args.force or env_bool("FORCE_RUN")

    if not force_run and now.hour != 9:
        print(f"Skipping: Lisbon local time is {now:%H:%M}, not 09:00.")
        return 0

    intervals_api_key = os.getenv("INTERVALS_ICU_API_KEY", "")
    openai_api_key = os.getenv("OPENAI_API_KEY", "")
    athlete_id = os.getenv("INTERVALS_ATHLETE_ID", "0")
    openai_model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    if args.dry_run:
        intervals_context = build_dry_run_context(target_date)
    else:
        if not intervals_api_key:
            raise RuntimeError("Missing INTERVALS_ICU_API_KEY.")

        intervals_client = IntervalsClient(intervals_api_key, athlete_id)
        intervals_context = fetch_intervals_context(intervals_client, target_date)

    if args.dry_run:
        ai_summary = build_fallback_summary(intervals_context)
    elif openai_api_key:
        ai_summary = build_ai_summary(openai_api_key, openai_model, intervals_context)
    else:
        print("Missing OPENAI_API_KEY. Writing fallback summary.")
        ai_summary = build_fallback_summary(intervals_context)

    week_path = get_week_path(target_date)
    existing_content = week_path.read_text(encoding="utf-8") if week_path.exists() else ""
    updated_content = upsert_day_block(existing_content, target_date, ai_summary)

    if updated_content == existing_content:
        print(f"No changes for {week_path}.")
        return 0

    if args.dry_run:
        print(updated_content)
        return 0

    week_path.parent.mkdir(parents=True, exist_ok=True)
    week_path.write_text(updated_content, encoding="utf-8")
    print(f"Updated {week_path.relative_to(REPO_ROOT)}.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Target Lisbon date in YYYY-MM-DD format.")
    parser.add_argument("--force", action="store_true", help="Run outside the 09:00 Lisbon window.")
    parser.add_argument("--dry-run", action="store_true", help="Print the generated file without API calls.")
    return parser.parse_args()


def parse_target_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


class IntervalsClient:
    def __init__(self, api_key: str, athlete_id: str) -> None:
        self.athlete_id = athlete_id
        token = base64.b64encode(f"API_KEY:{api_key}".encode("utf-8")).decode("ascii")
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Basic {token}",
        }

    def get_json(self, path: str, params: dict[str, str]) -> Any:
        query = urllib.parse.urlencode(params)
        url = f"{INTERVALS_BASE_URL}{path}?{query}"
        request = urllib.request.Request(url, headers=self.headers)

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Intervals.icu request failed: {error.code} {body}") from error

        return json.loads(payload) if payload else None

    def get_wellness(self, day: date) -> Any:
        return self.get_json(
            f"/athlete/{self.athlete_id}/wellness",
            {"oldest": day.isoformat(), "newest": day.isoformat()},
        )

    def get_activities(self, day: date) -> Any:
        return self.get_json(
            f"/athlete/{self.athlete_id}/activities",
            {"oldest": day.isoformat(), "newest": day.isoformat()},
        )


def fetch_intervals_context(client: IntervalsClient, target_date: date) -> dict[str, Any]:
    activity_date = target_date - timedelta(days=1)
    return {
        "target_date": target_date.isoformat(),
        "activity_date": activity_date.isoformat(),
        "wellness": safe_fetch(lambda: client.get_wellness(target_date), "wellness"),
        "activities": safe_fetch(lambda: client.get_activities(activity_date), "activities"),
    }


def safe_fetch(fetcher: Any, label: str) -> dict[str, Any]:
    try:
        return {"ok": True, "data": fetcher(), "error": None}
    except Exception as error:  # noqa: BLE001 - script must keep writing available context.
        return {"ok": False, "data": None, "error": f"{label}: {error}"}


def build_dry_run_context(target_date: date) -> dict[str, Any]:
    activity_date = target_date - timedelta(days=1)
    return {
        "target_date": target_date.isoformat(),
        "activity_date": activity_date.isoformat(),
        "wellness": {
            "ok": True,
            "data": [{"id": target_date.isoformat(), "sleep_secs": 27000, "resting_hr": 52, "hrv": 62}],
            "error": None,
        },
        "activities": {
            "ok": True,
            "data": [
                {
                    "id": "dry-run",
                    "name": "Easy Run",
                    "type": "Run",
                    "moving_time": 2700,
                    "distance": 8500,
                    "average_heartrate": 142,
                    "icu_training_load": 45,
                }
            ],
            "error": None,
        },
    }


def build_ai_summary(api_key: str, model: str, intervals_context: dict[str, Any]) -> dict[str, str]:
    compact_context = compact_intervals_context(intervals_context)
    body = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты endurance performance and health coach. Пиши кратко, по-русски, "
                    "без мотивационного шума. Не выдумывай отсутствующие данные. "
                    "Верни только JSON с ключами state и result."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Сформируй запись для недельного markdown-файла.\n"
                    "State: сон и восстановление за текущий день.\n"
                    "Result: активности за прошлый день и короткий вывод для планирования нагрузки.\n"
                    "Нужны только минимально полезные данные и аналитический вывод.\n\n"
                    f"Данные:\n{json.dumps(compact_context, ensure_ascii=False, indent=2)}"
                ),
            },
        ],
    }
    request = urllib.request.Request(
        OPENAI_CHAT_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return normalize_ai_summary(parsed)
    except Exception as error:  # noqa: BLE001 - fallback keeps the daily sync useful.
        print(f"OpenAI request failed. Writing fallback summary. Error: {error}", file=sys.stderr)
        return build_fallback_summary(intervals_context)


def normalize_ai_summary(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("OpenAI response is not a JSON object.")

    state = value.get("state")
    result = value.get("result")
    if not isinstance(state, str) or not isinstance(result, str):
        raise ValueError("OpenAI response must contain string keys: state, result.")

    return {"state": state.strip(), "result": result.strip()}


def compact_intervals_context(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "target_date": context["target_date"],
        "activity_date": context["activity_date"],
        "wellness": compact_wellness(context["wellness"]),
        "activities": compact_activities(context["activities"]),
    }


def compact_wellness(wellness_result: dict[str, Any]) -> Any:
    if not wellness_result["ok"]:
        return {"error": wellness_result["error"]}

    data = wellness_result["data"]
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return {"message": "No wellness data returned."}

    fields = [
        "id",
        "sleep_secs",
        "sleep_score",
        "resting_hr",
        "hrv",
        "hrv_rmssd",
        "fatigue",
        "stress",
        "readiness",
        "weight",
        "ctl",
        "atl",
        "form",
    ]
    return {field: data.get(field) for field in fields if data.get(field) is not None}


def compact_activities(activities_result: dict[str, Any]) -> Any:
    if not activities_result["ok"]:
        return {"error": activities_result["error"]}

    data = activities_result["data"]
    if not isinstance(data, list) or not data:
        return []

    fields = [
        "id",
        "name",
        "type",
        "moving_time",
        "elapsed_time",
        "distance",
        "total_elevation_gain",
        "average_heartrate",
        "max_heartrate",
        "average_watts",
        "weighted_average_watts",
        "average_speed",
        "icu_training_load",
        "training_load",
        "icu_intensity",
        "icu_atl",
        "icu_ctl",
        "icu_form",
    ]
    return [{field: item.get(field) for field in fields if item.get(field) is not None} for item in data]


def build_fallback_summary(context: dict[str, Any]) -> dict[str, str]:
    compact = compact_intervals_context(context)
    state = format_fallback_state(compact["wellness"])
    result = format_fallback_result(compact["activities"], context["activity_date"])
    return {"state": state, "result": result}


def format_fallback_state(wellness: Any) -> str:
    if isinstance(wellness, dict) and wellness.get("error"):
        return f"Данные восстановления не получены: {wellness['error']}"
    if isinstance(wellness, dict) and wellness.get("message"):
        return "Данные сна и восстановления в Intervals.icu не найдены. Готовность: неизвестно."
    if not isinstance(wellness, dict):
        return "Данные сна и восстановления в неожиданном формате. Готовность: неизвестно."

    parts = []
    if "sleep_secs" in wellness:
        parts.append(f"сон {seconds_to_hours(wellness['sleep_secs'])}")
    if "resting_hr" in wellness:
        parts.append(f"resting HR {wellness['resting_hr']}")
    if "hrv" in wellness:
        parts.append(f"HRV {wellness['hrv']}")
    if "hrv_rmssd" in wellness:
        parts.append(f"HRV RMSSD {wellness['hrv_rmssd']}")
    if "fatigue" in wellness:
        parts.append(f"fatigue {wellness['fatigue']}")
    if "readiness" in wellness:
        parts.append(f"readiness {wellness['readiness']}")

    summary = ", ".join(parts) if parts else "ключевые recovery-поля не заполнены"
    return f"Intervals.icu: {summary}. Готовность: оценить консервативно."


def format_fallback_result(activities: Any, activity_date: str) -> str:
    if isinstance(activities, dict) and activities.get("error"):
        return f"Активности за {activity_date} не получены: {activities['error']}"
    if not activities:
        return f"За {activity_date} активности в Intervals.icu не найдены. Анализ: дополнительной тренировочной нагрузки не зафиксировано."
    if not isinstance(activities, list):
        return f"Активности за {activity_date} пришли в неожиданном формате. Анализ: не использовать для повышения нагрузки."

    lines = []
    for activity in activities:
        if not isinstance(activity, dict):
            continue
        lines.append(format_activity(activity))

    joined = "\n".join(f"- {line}" for line in lines) if lines else "Нет пригодных активностей."
    return f"{joined}\nАнализ: использовать как raw summary; финальную нагрузку повышать только после проверки восстановления."


def format_activity(activity: dict[str, Any]) -> str:
    parts = [str(activity.get("name") or activity.get("type") or "Activity")]
    if "moving_time" in activity:
        parts.append(f"duration {seconds_to_hhmm(activity['moving_time'])}")
    if "distance" in activity:
        parts.append(f"distance {meters_to_km(activity['distance'])}")
    if "total_elevation_gain" in activity:
        parts.append(f"elevation {round_number(activity['total_elevation_gain'])} m")
    if "average_heartrate" in activity:
        parts.append(f"avg HR {activity['average_heartrate']}")
    if "average_watts" in activity:
        parts.append(f"avg power {activity['average_watts']} W")
    load = activity.get("icu_training_load", activity.get("training_load"))
    if load is not None:
        parts.append(f"load {round_number(load)}")
    return ", ".join(parts)


def seconds_to_hours(value: Any) -> str:
    try:
        return f"{float(value) / 3600:.1f} h"
    except (TypeError, ValueError):
        return str(value)


def seconds_to_hhmm(value: Any) -> str:
    try:
        total_minutes = round(float(value) / 60)
    except (TypeError, ValueError):
        return str(value)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}:{minutes:02d}"


def meters_to_km(value: Any) -> str:
    try:
        return f"{float(value) / 1000:.2f} km"
    except (TypeError, ValueError):
        return str(value)


def round_number(value: Any) -> str:
    try:
        return str(round(float(value)))
    except (TypeError, ValueError):
        return str(value)


def get_week_path(day: date) -> Path:
    week_number = day.isocalendar().week
    return REPO_ROOT / "training" / str(day.year) / "weeks" / f"w{week_number:02d}.md"


def upsert_day_block(content: str, day: date, ai_summary: dict[str, str]) -> str:
    heading = format_day_heading(day)
    week_header = format_week_header(day)
    normalized = ensure_week_header(content, week_header)
    existing_block = extract_day_block(normalized, heading)
    plan = extract_plan(existing_block) if existing_block else "TBD"
    new_block = format_day_block(heading, plan, ai_summary["state"], ai_summary["result"])

    if existing_block:
        return normalized.replace(existing_block, new_block)

    separator = "" if normalized.endswith("\n\n") else "\n"
    return f"{normalized}{separator}{new_block}\n"


def ensure_week_header(content: str, week_header: str) -> str:
    stripped = content.strip()
    if stripped:
        return content if content.endswith("\n") else f"{content}\n"
    return f"{week_header}\n\n"


def format_week_header(day: date) -> str:
    return (
        f"# Week {day.isocalendar().week:02d} / {day.year}\n\n"
        "Weekly goal:\n\n"
        "Key risks:\n\n"
        "Planned load:\n\n"
        "Actual load:\n\n"
        "Next-week decision:"
    )


def format_day_heading(day: date) -> str:
    return f"{day.day} {RUSSIAN_MONTHS[day.month]}, {RUSSIAN_WEEKDAYS[day.weekday()]}"


def extract_day_block(content: str, heading: str) -> str | None:
    pattern = re.compile(rf"^## {re.escape(heading)}\n.*?(?=^## |\Z)", re.MULTILINE | re.DOTALL)
    match = pattern.search(content)
    return match.group(0).rstrip("\n") if match else None


def extract_plan(block: str) -> str:
    match = re.search(r"^Plan:\n(.*?)(?=^State:|\Z)", block, re.MULTILINE | re.DOTALL)
    if not match:
        return "TBD"
    plan = match.group(1).strip()
    return plan or "TBD"


def format_day_block(heading: str, plan: str, state: str, result: str) -> str:
    return (
        f"## {heading}\n\n"
        "Plan:\n"
        f"{plan.strip()}\n\n"
        "State:\n"
        f"{state.strip()}\n\n"
        "Result:\n"
        f"{result.strip()}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
