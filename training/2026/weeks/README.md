# Weekly Training Files

Create one file per calendar week.

Filename:

`w{week-number}.md`

Examples:

- `w01.md`
- `w21.md`
- `w52.md`

## Purpose

Weekly files are the long-term memory for training load planning.

Use them to connect:

- the planned load
- readiness and recovery state
- actual completed training
- the coaching decision for the next week

Do not duplicate full raw activity data. Pull raw data from Intervals.icu or Garmin when needed and store only the minimum useful summary here.

## Automated Daily Update

The GitHub Action in `.github/workflows/daily-intervals-sync.yml` updates the current week file once per day.

At 09:00 Europe/Lisbon it should write:

- `State`: current-day sleep and recovery data from Intervals.icu
- `Result`: previous-day activity data from Intervals.icu plus short coaching analysis

If a day block already exists, the automation preserves `Plan` and replaces only `State` and `Result`.

## Weekly Header Template

```md
# Week {week-number} / {year}

Weekly goal:

Key risks:

Planned load:

Actual load:

Next-week decision:
```

## Daily Template

```md
## {day-number} {month}, {weekday}

Plan:
{Planned session for the day. Include purpose, duration, intensity, and adjustment rule when needed.}

State:
{Brief sleep and recovery analysis from Intervals.icu and subjective context. Include readiness: high / medium / low.}

Result:
{Minimum useful Intervals.icu activity data plus short coaching analysis for future planning.}
```

## Result Guidance

Include only data that changes future planning decisions.

Useful fields:

- session type
- duration
- distance
- elevation
- average HR
- max HR when relevant
- pace or power when relevant
- training load
- time in zones when relevant
- fitness / fatigue / form when available
- pain, soreness, or unusual fatigue
- fueling or hydration issue if it affected the session

End each result with a short decision-oriented analysis:

`Analysis: absorbed well / manageable fatigue / excess load / downgrade next session / deload needed.`
