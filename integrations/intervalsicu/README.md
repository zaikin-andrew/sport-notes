# Intervals.icu Integration

Goal: use Intervals.icu as the analytics source for training load, fitness, fatigue, form, HR, power, pace, and workout history.

## Desired Workflow

1. Pull training data from Intervals.icu.
2. Summarize recent training load and key workouts.
3. Update the active weekly file in `training/2026/weeks/` through reviewed diffs.
4. Do not manually duplicate raw data unless needed.
5. Use repository files for decisions, plans, and reviews.

## Useful Data To Pull

- recent activities
- weekly volume
- running distance
- cycling distance
- time in zones
- training load
- fitness/fatigue/form
- HR trends
- power trends
- pace trends
- workout notes

## Weekly File Update Rules

Use Intervals.icu to fill the `State` and `Result` sections in weekly files.

For `State`, summarize sleep, recovery, HRV, resting HR, fatigue, and readiness when available.

For `Result`, store only the minimum activity data needed for future planning, plus a short coaching analysis.

## Future MCP Ideas

Tools could expose:

- get_recent_activities(days)
- get_week_summary(start, end)
- get_activity(activity_id)
- get_fitness_trend(days)
- get_time_in_zones(start, end)
- generate_context_summary(days)
