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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


LISBON_TZ = ZoneInfo("Europe/Lisbon")
REPO_ROOT = Path(__file__).resolve().parents[1]
INTERVALS_BASE_URL = "https://intervals.icu/api/v1"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
HTTP_USER_AGENT = "sport-notes-intervals-sync/1.0"
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


def log(message: str) -> None:
    print(f"[intervals-sync] {message}", file=sys.stderr)


def main() -> int:
    args = parse_args()
    now = datetime.now(LISBON_TZ)
    scheduled_now = get_scheduled_lisbon_time(now)
    target_date = parse_target_date(args.date) if args.date else now.date()
    force_run = args.force or env_bool("FORCE_RUN")

    log(
        "start "
        f"now_lisbon={now.isoformat()} "
        f"scheduled_lisbon={scheduled_now.isoformat()} "
        f"target_date={target_date.isoformat()} "
        f"force_run={force_run} "
        f"dry_run={args.dry_run}"
    )

    if not force_run and scheduled_now.hour != 9:
        log(
            "Skipping: scheduled Lisbon local time is "
            f"{scheduled_now:%H:%M}, not 09:00."
        )
        return 0

    intervals_api_key = os.getenv("INTERVALS_ICU_API_KEY", "")
    openai_api_key = os.getenv("OPENAI_API_KEY", "")
    athlete_id = os.getenv("INTERVALS_ATHLETE_ID", "0")
    openai_model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    log(
        "config "
        f"athlete_id={athlete_id} "
        f"openai_model={openai_model} "
        f"has_intervals_key={bool(intervals_api_key)} "
        f"has_openai_key={bool(openai_api_key)}"
    )

    if args.dry_run:
        intervals_context = build_dry_run_context(target_date)
    else:
        if not intervals_api_key:
            raise RuntimeError("Missing INTERVALS_ICU_API_KEY.")

        intervals_client = IntervalsClient(intervals_api_key, athlete_id)
        intervals_context = fetch_intervals_context(intervals_client, target_date)

    log_json("compact_intervals_context", compact_intervals_context(intervals_context))

    if args.dry_run:
        ai_summary = build_fallback_summary(intervals_context)
    elif openai_api_key:
        ai_summary = build_ai_summary(openai_api_key, openai_model, intervals_context)
    else:
        print("Missing OPENAI_API_KEY. Writing fallback summary.")
        ai_summary = build_fallback_summary(intervals_context)

    updates = build_week_updates(target_date, ai_summary)

    if not updates:
        print("No weekly file changes.")
        return 0

    if args.dry_run:
        for path, content in updates.items():
            print(f"--- {path.relative_to(REPO_ROOT)} ---")
            print(content)
        return 0

    for path, content in updates.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"Updated {path.relative_to(REPO_ROOT)}.")
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


def get_scheduled_lisbon_time(now: datetime) -> datetime:
    schedule = os.getenv("GITHUB_EVENT_SCHEDULE", "").strip()
    match = re.fullmatch(r"(\d+)\s+(\d+)\s+\*\s+\*\s+\*", schedule)
    if not match:
        return now

    minute = int(match.group(1))
    hour = int(match.group(2))
    scheduled_utc = datetime.combine(
        now.astimezone(timezone.utc).date(),
        datetime.min.time(),
        tzinfo=timezone.utc,
    ).replace(hour=hour, minute=minute)

    return scheduled_utc.astimezone(LISBON_TZ)


def summarize_http_error(status_code: int, body: str) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return f"Intervals.icu request failed: {status_code} {body[:500]}"

    title = payload.get("title")
    detail = payload.get("detail")
    error_name = payload.get("error_name")
    error_code = payload.get("error_code")
    parts = [f"Intervals.icu request failed: {status_code}"]

    if title:
        parts.append(str(title))
    if error_name:
        parts.append(f"error_name={error_name}")
    if error_code:
        parts.append(f"error_code={error_code}")
    if detail:
        parts.append(str(detail))

    return " | ".join(parts)


def log_json(label: str, value: Any) -> None:
    formatted = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    log(f"{label}={formatted}")


def log_keys(label: str, value: dict[str, Any]) -> None:
    keys = sorted(value.keys())
    log(f"{label}={json.dumps(keys, ensure_ascii=False)}")


class IntervalsClient:
    def __init__(self, api_key: str, athlete_id: str) -> None:
        self.athlete_id = athlete_id
        token = base64.b64encode(f"API_KEY:{api_key}".encode("utf-8")).decode("ascii")
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Basic {token}",
            "User-Agent": HTTP_USER_AGENT,
        }

    def get_json(self, path: str, params: dict[str, str]) -> Any:
        query = urllib.parse.urlencode(params)
        url = f"{INTERVALS_BASE_URL}{path}?{query}"
        log(f"intervals_request method=GET path={path} params={json.dumps(params, sort_keys=True)}")
        request = urllib.request.Request(url, headers=self.headers)

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read().decode("utf-8")
                log(
                    "intervals_response "
                    f"path={path} status={response.status} bytes={len(payload.encode('utf-8'))}"
                )
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            message = summarize_http_error(error.code, body)
            log(f"intervals_response path={path} status={error.code} error={message}")
            raise RuntimeError(message) from error

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

    def get_activity_detail(self, activity_id: str) -> Any:
        return self.get_json(f"/activity/{activity_id}", {"intervals": "true"})


def fetch_intervals_context(client: IntervalsClient, target_date: date) -> dict[str, Any]:
    activity_date = target_date - timedelta(days=1)
    log(f"fetch_context wellness_date={target_date.isoformat()} activity_date={activity_date.isoformat()}")
    activities = safe_fetch(lambda: client.get_activities(activity_date), "activities")
    return {
        "target_date": target_date.isoformat(),
        "activity_date": activity_date.isoformat(),
        "wellness": safe_fetch(lambda: client.get_wellness(target_date), "wellness"),
        "activities": activities,
        "activity_details": fetch_activity_details(client, activities),
    }


def fetch_activity_details(client: IntervalsClient, activities_result: dict[str, Any]) -> dict[str, Any]:
    if not activities_result["ok"]:
        return {"ok": False, "data": None, "error": activities_result["error"]}

    activities = activities_result["data"]
    if not isinstance(activities, list) or not activities:
        return {"ok": True, "data": [], "error": None}

    details = []
    for activity in activities:
        if not isinstance(activity, dict) or not activity.get("id"):
            continue

        activity_id = str(activity["id"])
        detail = safe_fetch(lambda activity_id=activity_id: client.get_activity_detail(activity_id), f"activity_detail:{activity_id}")
        details.append({"id": activity_id, **detail})

    return {"ok": True, "data": details, "error": None}


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
        "activity_details": {
            "ok": True,
            "data": [
                {
                    "id": "dry-run",
                    "ok": True,
                    "data": {
                        "id": "dry-run",
                        "icu_hr_zone_times": [0, 2100, 600, 0, 0],
                        "decoupling": 2.4,
                        "icu_intervals": [
                            {
                                "type": "WORK",
                                "moving_time": 2700,
                                "distance": 8500,
                                "average_heartrate": 142,
                                "training_load": 45,
                                "zone": 2,
                            }
                        ],
                    },
                    "error": None,
                }
            ],
            "error": None,
        },
    }


def build_ai_summary(api_key: str, model: str, intervals_context: dict[str, Any]) -> dict[str, str]:
    compact_context = compact_intervals_context(intervals_context)
    target_label = format_day_heading(parse_target_date(compact_context["target_date"]))
    activity_label = format_day_heading(parse_target_date(compact_context["activity_date"]))
    log(
        "openai_request "
        f"method=POST url={OPENAI_CHAT_URL} model={model} "
        "temperature=0.2 response_format=json_object"
    )
    log_json("openai_prompt_context", compact_context)
    body = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты endurance performance and health coach. Приоритеты: здоровье, восстановление, "
                    "профилактика травм, стабильная аэробная база, затем производительность. "
                    "Пиши кратко, по-русски, без мотивационного шума. "
                    "Давай консервативное решение при плохом сне, высокой усталости, боли или неполных данных. "
                    "Анализируй нагрузку через ATL/CTL/form, training load, длительность, HR, зоны, "
                    "decoupling/дрейф, рельеф и накопленную усталость. "
                    "Если ATL заметно выше CTL, form отрицательный, сон плохой/неизвестен или HRV низкий, "
                    "не рекомендуй интенсивность. После long ride, high load activity или высокой доли Z3+ "
                    "на следующий день предпочитай Z1/Z2, mobility или отдых. "
                    "Вывод должен помогать решить: keep / reduce duration / reduce intensity / Z1-Z2 only / mobility / full rest. "
                    "Не выдумывай отсутствующие данные. "
                    "Верни только JSON с ключами state и result."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Сформируй запись для недельного markdown-файла.\n"
                    f"State: сон и восстановление за {target_label}. Обязательно явно укажи "
                    "\"Длительность сна\" и \"средний пульс\"; если данных нет, напиши \"нет данных\".\n"
                    f"Result: активности за {activity_label} и короткий вывод, что учесть на {target_label}.\n"
                    "В Result не добавляй префикс вида \"Активность YYYY-MM-DD\", потому что запись "
                    "будет помещена в блок нужного дня.\n"
                    "Не используй технические имена переменных target_date и activity_date в ответе.\n"
                    "Для активности указывай длительность, дистанцию, набор, средний пульс, max HR "
                    "и нагрузку, если эти поля есть.\n"
                    "Если есть зоны, интервалы или decoupling, используй их для вывода, но не перечисляй сырые массивы.\n"
                    "Result должен заканчиваться коротким решением для следующего дня: что ограничить или разрешить.\n"
                    "State и Result будут записаны в разные дневные блоки.\n"
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
            response_body = response.read().decode("utf-8")
            log(f"openai_response status={response.status} bytes={len(response_body.encode('utf-8'))}")
            payload = json.loads(response_body)
        content = payload["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return normalize_ai_summary(parsed, compact_context["target_date"], compact_context["activity_date"])
    except Exception as error:  # noqa: BLE001 - fallback keeps the daily sync useful.
        print(f"OpenAI request failed. Writing fallback summary. Error: {error}", file=sys.stderr)
        return build_fallback_summary(intervals_context)


def normalize_ai_summary(value: Any, target_date: str, activity_date: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("OpenAI response is not a JSON object.")

    state = value.get("state")
    result = value.get("result")
    if not isinstance(state, str) or not isinstance(result, str):
        raise ValueError("OpenAI response must contain string keys: state, result.")

    return {
        "state": replace_date_placeholders(state.strip(), target_date, activity_date),
        "result": replace_date_placeholders(result.strip(), target_date, activity_date),
    }


def replace_date_placeholders(text: str, target_date: str, activity_date: str) -> str:
    target_label = format_day_heading(parse_target_date(target_date))
    activity_label = format_day_heading(parse_target_date(activity_date))
    return text.replace("target_date", target_label).replace("activity_date", activity_label)


def compact_intervals_context(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "target_date": context["target_date"],
        "activity_date": context["activity_date"],
        "wellness": compact_wellness(context["wellness"]),
        "activities": compact_activities(context["activities"]),
        "activity_details": compact_activity_details(context.get("activity_details")),
    }


def compact_wellness(wellness_result: dict[str, Any]) -> Any:
    if not wellness_result["ok"]:
        return {"error": wellness_result["error"]}

    data = wellness_result["data"]
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return {"message": "No wellness data returned."}

    log_keys("raw_wellness_keys", data)
    fields = [
        "id",
        "date",
        "sleep_secs",
        "sleepSecs",
        "sleep_time",
        "sleepTime",
        "sleep_duration",
        "sleepDuration",
        "sleep_score",
        "sleepScore",
        "sleep_quality",
        "sleepQuality",
        "average_hr",
        "avg_hr",
        "average_heartrate",
        "avgSleepingHR",
        "averageSleepingHR",
        "lowestSleepingHR",
        "max_hr",
        "maxHr",
        "min_hr",
        "minHr",
        "resting_hr",
        "restingHR",
        "hrv",
        "hrv_rmssd",
        "hrvRmssd",
        "hrv_sdnn",
        "hrvSdnn",
        "fatigue",
        "soreness",
        "stress",
        "mood",
        "motivation",
        "readiness",
        "recovery",
        "weight",
        "bodyFat",
        "steps",
        "calories",
        "ctl",
        "atl",
        "form",
        "rampRate",
        "ctlLoad",
        "atlLoad",
        "sportInfo",
    ]
    return {field: data.get(field) for field in fields if data.get(field) is not None}


def compact_activities(activities_result: dict[str, Any]) -> Any:
    if not activities_result["ok"]:
        return {"error": activities_result["error"]}

    data = activities_result["data"]
    if not isinstance(data, list) or not data:
        return []

    for index, item in enumerate(data):
        if isinstance(item, dict):
            log_keys(f"raw_activity_keys[{index}]", item)

    fields = [
        "id",
        "name",
        "type",
        "sport",
        "sub_type",
        "subType",
        "start_date_local",
        "startDateLocal",
        "start_date",
        "startDate",
        "end_date_local",
        "endDateLocal",
        "moving_time",
        "movingTime",
        "elapsed_time",
        "elapsedTime",
        "distance",
        "total_elevation_gain",
        "totalElevationGain",
        "elevation_gain",
        "elevationGain",
        "average_heartrate",
        "averageHeartrate",
        "max_heartrate",
        "maxHeartrate",
        "min_heartrate",
        "minHeartrate",
        "average_watts",
        "averageWatts",
        "weighted_average_watts",
        "weightedAverageWatts",
        "icu_weighted_avg_watts",
        "icuWeightedAvgWatts",
        "normalized_power",
        "normalizedPower",
        "max_watts",
        "maxWatts",
        "average_speed",
        "averageSpeed",
        "max_speed",
        "maxSpeed",
        "average_cadence",
        "averageCadence",
        "max_cadence",
        "maxCadence",
        "icu_training_load",
        "icuTrainingLoad",
        "training_load",
        "trainingLoad",
        "icu_intensity",
        "icuIntensity",
        "icu_atl",
        "icuAtl",
        "icu_ctl",
        "icuCtl",
        "icu_form",
        "icuForm",
        "icu_ramp_rate",
        "icuRampRate",
        "icu_ftp",
        "icuFtp",
        "icu_weighted_avg_pace",
        "icuWeightedAvgPace",
        "icu_grade_adjusted_distance",
        "icuGradeAdjustedDistance",
        "icu_power_hr",
        "icuPowerHr",
        "decoupling",
        "pa_decoupling",
        "power_hr",
        "powerHr",
        "calories",
        "kilojoules",
        "joules",
        "perceived_exertion",
        "perceivedExertion",
        "rpe",
        "feel",
        "feeling",
        "description",
        "notes",
        "commute",
        "trainer",
        "device_name",
        "deviceName",
        "gear",
        "icu_zone_times",
        "icuZoneTimes",
        "zone_times",
        "zoneTimes",
        "icu_hr_zone_times",
        "icuHrZoneTimes",
        "hr_zone_times",
        "hrZoneTimes",
        "pace_zone_times",
        "paceZoneTimes",
        "power_zone_times",
        "powerZoneTimes",
    ]
    return [{field: item.get(field) for field in fields if item.get(field) is not None} for item in data]


def compact_activity_details(details_result: Any) -> Any:
    if not isinstance(details_result, dict):
        return {"message": "No activity detail request was made."}
    if not details_result["ok"]:
        return {"error": details_result["error"]}

    details = details_result["data"]
    if not isinstance(details, list) or not details:
        return []

    compacted = []
    for detail_result in details:
        if not isinstance(detail_result, dict):
            continue

        activity_id = detail_result.get("id")
        if not detail_result.get("ok"):
            compacted.append({"id": activity_id, "error": detail_result.get("error")})
            continue

        detail = detail_result.get("data")
        if not isinstance(detail, dict):
            compacted.append({"id": activity_id, "message": "No detail data returned."})
            continue

        log_keys(f"raw_activity_detail_keys[{activity_id}]", detail)
        compacted.append(compact_activity_detail(detail))

    return compacted


def compact_activity_detail(detail: dict[str, Any]) -> dict[str, Any]:
    fields = [
        "id",
        "name",
        "type",
        "description",
        "notes",
        "moving_time",
        "elapsed_time",
        "distance",
        "total_elevation_gain",
        "average_heartrate",
        "max_heartrate",
        "average_watts",
        "weighted_average_watts",
        "icu_weighted_avg_watts",
        "normalized_power",
        "average_speed",
        "average_cadence",
        "max_cadence",
        "icu_training_load",
        "training_load",
        "icu_intensity",
        "icu_atl",
        "icu_ctl",
        "icu_form",
        "icu_ramp_rate",
        "decoupling",
        "pa_decoupling",
        "icu_power_hr",
        "power_hr",
        "calories",
        "kilojoules",
        "perceived_exertion",
        "icu_zone_times",
        "zone_times",
        "icu_hr_zone_times",
        "hr_zone_times",
        "icu_power_zone_times",
        "power_zone_times",
        "icu_pace_zone_times",
        "pace_zone_times",
    ]
    compacted = {field: detail.get(field) for field in fields if detail.get(field) is not None}

    intervals = detail.get("icu_intervals")
    if isinstance(intervals, list):
        compacted["interval_summary"] = summarize_intervals(intervals)

    return compacted


def summarize_intervals(intervals: list[Any]) -> dict[str, Any]:
    valid_intervals = [interval for interval in intervals if isinstance(interval, dict)]
    work_intervals = [interval for interval in valid_intervals if str(interval.get("type", "")).upper() == "WORK"]
    load_values = [to_float(interval.get("training_load")) for interval in valid_intervals]
    load_values = [value for value in load_values if value is not None]

    return {
        "count": len(valid_intervals),
        "work_count": len(work_intervals),
        "total_work_time": sum_numeric(work_intervals, "moving_time"),
        "total_work_distance": sum_numeric(work_intervals, "distance"),
        "total_interval_load": round(sum(load_values), 1) if load_values else None,
        "hardest_intervals": select_hardest_intervals(work_intervals or valid_intervals),
    }


def select_hardest_intervals(intervals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sorted_intervals = sorted(
        intervals,
        key=lambda interval: to_float(interval.get("training_load")) or 0,
        reverse=True,
    )
    fields = [
        "type",
        "moving_time",
        "distance",
        "average_heartrate",
        "max_heartrate",
        "average_watts",
        "weighted_average_watts",
        "training_load",
        "zone",
        "decoupling",
    ]
    return [
        {field: interval.get(field) for field in fields if interval.get(field) is not None}
        for interval in sorted_intervals[:5]
    ]


def build_fallback_summary(context: dict[str, Any]) -> dict[str, str]:
    compact = compact_intervals_context(context)
    state = format_fallback_state(compact["wellness"])
    result = format_fallback_result(compact["activities"], context["activity_date"])
    return {"state": state, "result": result}


def format_fallback_state(wellness: Any) -> str:
    if isinstance(wellness, dict) and wellness.get("error"):
        return (
            "Длительность сна: нет данных. Средний пульс: нет данных. "
            f"Данные восстановления не получены: {wellness['error']}"
        )
    if isinstance(wellness, dict) and wellness.get("message"):
        return (
            "Длительность сна: нет данных. Средний пульс: нет данных. "
            "Данные сна и восстановления в Intervals.icu не найдены. Готовность: неизвестно."
        )
    if not isinstance(wellness, dict):
        return (
            "Длительность сна: нет данных. Средний пульс: нет данных. "
            "Данные сна и восстановления в неожиданном формате. Готовность: неизвестно."
        )

    parts = []
    if "sleep_secs" in wellness:
        parts.append(f"Длительность сна: {seconds_to_hours(wellness['sleep_secs'])}")
    else:
        parts.append("Длительность сна: нет данных")

    average_hr = first_present(wellness, ["average_hr", "avg_hr", "average_heartrate"])
    if average_hr is not None:
        parts.append(f"средний пульс: {average_hr}")
    else:
        parts.append("средний пульс: нет данных")

    if "resting_hr" in wellness:
        parts.append(f"пульс покоя: {wellness['resting_hr']}")
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
        return f"Активности не получены: {activities['error']}"
    if not activities:
        return "Активности в Intervals.icu не найдены. Анализ: дополнительной тренировочной нагрузки не зафиксировано."
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
        parts.append(f"длительность {seconds_to_hhmm(activity['moving_time'])}")
    if "distance" in activity:
        parts.append(f"дистанция {meters_to_km(activity['distance'])}")
    if "total_elevation_gain" in activity:
        parts.append(f"набор {round_number(activity['total_elevation_gain'])} м")
    if "average_heartrate" in activity:
        parts.append(f"средний пульс {activity['average_heartrate']}")
    if "max_heartrate" in activity:
        parts.append(f"max HR {activity['max_heartrate']}")
    if "average_watts" in activity:
        parts.append(f"средняя мощность {activity['average_watts']} W")
    load = activity.get("icu_training_load", activity.get("training_load"))
    if load is not None:
        parts.append(f"нагрузка {round_number(load)}")
    return ", ".join(parts)


def first_present(source: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in source and source[key] is not None:
            return source[key]
    return None


def to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def sum_numeric(items: list[dict[str, Any]], key: str) -> float | None:
    values = [to_float(item.get(key)) for item in items]
    values = [value for value in values if value is not None]
    return round(sum(values), 1) if values else None


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


def build_week_updates(target_date: date, ai_summary: dict[str, str]) -> dict[Path, str]:
    activity_date = target_date - timedelta(days=1)
    pending: dict[Path, str] = {}

    target_path = get_week_path(target_date)
    target_content = read_pending_or_file(pending, target_path)
    pending[target_path] = upsert_day_field(target_content, target_date, state=ai_summary["state"])

    activity_path = get_week_path(activity_date)
    activity_content = read_pending_or_file(pending, activity_path)
    pending[activity_path] = upsert_day_field(activity_content, activity_date, result=ai_summary["result"])

    changed = {}
    for path, updated_content in pending.items():
        existing_content = path.read_text(encoding="utf-8") if path.exists() else ""
        if updated_content != existing_content:
            changed[path] = updated_content
            log(f"week_update path={path.relative_to(REPO_ROOT)} changed=true")
        else:
            log(f"week_update path={path.relative_to(REPO_ROOT)} changed=false")

    return changed


def read_pending_or_file(pending: dict[Path, str], path: Path) -> str:
    if path in pending:
        return pending[path]
    return path.read_text(encoding="utf-8") if path.exists() else ""


def upsert_day_field(
    content: str,
    day: date,
    *,
    state: str | None = None,
    result: str | None = None,
) -> str:
    heading = format_day_heading(day)
    normalized = ensure_week_structure(content, day)
    existing_block = extract_day_block(normalized, heading)
    plan = extract_plan(existing_block) if existing_block else "TBD"
    current_state = extract_state(existing_block) if existing_block else "TBD"
    current_result = extract_result(existing_block) if existing_block else "TBD"
    new_block = format_day_block(
        heading,
        plan,
        state if state is not None else current_state,
        result if result is not None else current_result,
    )

    if existing_block:
        return normalized.replace(existing_block, new_block)

    separator = "" if normalized.endswith("\n\n") else "\n"
    return f"{normalized}{separator}{new_block}\n"


def ensure_week_structure(content: str, week_reference_day: date) -> str:
    header = extract_week_header(content) or format_week_header(week_reference_day)
    blocks = []

    for day in week_days(week_reference_day):
        heading = format_day_heading(day)
        existing_block = extract_day_block(content, heading)
        blocks.append(
            format_day_block(
                heading,
                extract_plan(existing_block),
                extract_state(existing_block),
                extract_result(existing_block),
            )
        )

    return f"{header.strip()}\n\n" + "\n\n".join(blocks) + "\n"


def extract_week_header(content: str) -> str | None:
    stripped = content.strip()
    if not stripped:
        return None

    match = re.search(r"^## ", stripped, re.MULTILINE)
    if not match:
        return stripped

    header = stripped[: match.start()].strip()
    return header or None


def week_days(day: date) -> list[date]:
    monday = day - timedelta(days=day.weekday())
    return [monday + timedelta(days=offset) for offset in range(7)]


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


def extract_plan(block: str | None) -> str:
    return extract_section(block, "Plan")


def extract_state(block: str | None) -> str:
    return extract_section(block, "State")


def extract_result(block: str | None) -> str:
    return extract_section(block, "Result")


def extract_section(block: str | None, section_name: str) -> str:
    if not block:
        return "TBD"

    match = re.search(
        rf"^{re.escape(section_name)}:\n(.*?)(?=^(?:Plan|State|Result):|\Z)",
        block,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        return "TBD"
    value = match.group(1).strip()
    return value or "TBD"


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
