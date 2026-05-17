# Fitness Coach Repository

This repository is designed to be used with Cursor as a source of truth for endurance training, recovery, nutrition, mobility, and bodyweight strength planning.

## Purpose

The goal is to create a stable AI coaching environment where Cursor can read project rules and current athlete context before generating plans, reviewing workouts, or analyzing nutrition.

## Core Idea

Separate permanent coaching rules from changing athlete data.

- `.cursor/rules/` contains stable coaching behavior.
- `docs/` contains athlete profile, current goals, zones, constraints, and decision context.
- `training/` contains weekly training memory, workout reviews, and race context.
- `recovery/` contains fatigue, sleep, and illness notes.
- `docs/nutrition/` contains food logs and body composition context.
- `integrations/` contains notes for Garmin, Intervals.icu, and future automation.

## Cursor Usage

Cursor should treat `.cursor/rules/*.mdc` as system-level coaching rules.

Before generating a plan or review, read:

1. `docs/athlete-profile.md`
2. `docs/current-goals.md`
3. `docs/constraints.md`
4. `docs/weekly-structure.md`
5. `docs/zones.md`
6. recent files in `training/2026/weeks/`
7. recent files in `training/2026/workout-reviews/`
8. relevant files in `recovery/`
9. relevant files in `docs/nutrition/`

## Important Principle

Do not duplicate raw training data manually if it can be pulled from Garmin or Intervals.icu.

Use this repository for decisions, context, summaries, reviews, and plan logic.

Weekly files in `training/2026/weeks/` are the main long-term memory for planning load. Use one `w{week-number}.md` file per calendar week and store only the minimum useful summary needed for future decisions.
