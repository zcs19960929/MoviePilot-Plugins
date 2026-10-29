# MetaTube 1.1.2 Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline. The user approved direct implementation of this design; no additional handoff approval is needed.

**Goal:** Add accessible scheduling and readable Jellyfin movie information without changing cloud operations.

**Architecture:** A small scheduling module translates saved configuration into the existing host service contract and validates advanced expressions with the host's APScheduler. Existing NFO construction adds presentation data; the direct pipeline compares regenerated NFO bytes before skipping a completed job.

**Tech Stack:** Python, APScheduler 3 supplied by MoviePilot, XML ElementTree, unittest, existing Node form evaluator.

**Spec:** ../specs/2026-10-29-metatube-schedule-nfo-design.md

## Global Constraints

- Mirror V2/V3; version 1.1.2 in code and both manifests; no new runtime dependency or API route.
- Preserve all saved configuration and legacy daily scheduling, including zero hour/minute.
- Only manual execution remains possible when scheduling is invalid; show the scheduling error without starting a background timer.
- Retain original descriptions and standard NFO fields; omit unavailable data.
- Respect file ownership/digests and resumability; no NAS configuration changes or live scraping in this run.

## Review Focus

- A missing schedule mode in existing configuration must stay daily rather than disable automation.
- Invalid inactive fields must not break the selected mode or manual scraping.
- Zero values, dropdown objects, whitespace, malformed expressions, and numeric weekdays must have explicit semantics.
- Re-rendering old owned NFOs must be idempotent, retain stable playback identifiers and avoid cloud writes/downloads.
- Modified files, interrupted publication, invalid dates/ratings, and XML characters must remain safe.

### Task 1: Host scheduling and linked form

Files: both `metatubescraper/schedule.py` (new), both `__init__.py`, `tests/test_metatube_schedule.py` (new), `tests/test_metatube_form.py`.

Interface: `build_schedule(config: dict) -> Optional[dict]` returns the host `trigger` and `kwargs`, or raises `ValueError` with a concise Chinese explanation. It does not register or run a job.

- [x] Add failing tests for five modes, legacy zero values, host timezone, invalid inactive values, Cron errors, dropdown shapes and form field retention.
- [x] Run the new tests and confirm feature-related failures.
- [x] Implement the translator, call it from `get_service`, and display errors on the existing detail page. Add reactive schedule fields and concise examples.
- [x] Run scheduling/form tests with real APScheduler; confirm manual API is unaffected by invalid scheduling.

### Task 2: NFO presentation and safe upgrades

Files: both `__init__.py`, both `direct.py`, `tests/test_metatube_nfo.py` (new), `tests/test_metatube_direct.py`.

Interface: retain `__build_nfo(info, number) -> bytes`. Add direct `_render_nfo(info, number) -> bytes` to share local artwork rendering with the completed-job comparison.

- [x] Add failing tests for canonical title prefix, original title, complete facts + original summary, absent/invalid fields, XML escaping and Jellyfin-compatible rating values.
- [x] Add failing integration tests for updating an owned old NFO from cached metadata without downloading or renaming, a second-run skip, partial publication recovery and externally modified NFO protection.
- [x] Run and confirm the expected failures; implement the smallest changes and rerun.

### Task 3: Release metadata, regression and review

Files: README, CHANGELOG, both manifests, version constants and release consistency tests.

- [x] Bump to 1.1.2 and document schedule examples, Cron weekdays, NFO behavior and Jellyfin local metadata refresh.
- [x] Run all Token, SDK, direct, host, form and new scheduling/NFO tests; compare V2/V3 source parity and manifest histories.
- [x] Review the complete diff with a fresh reviewer, resolve material issues, and rerun relevant checks.
- [x] Report local implementation and actual test evidence, clearly separating them from unperformed deployment and Jellyfin UI verification.

Validation: 59 unittest cases passed with real APScheduler 3 and the existing pinned 115 SDK. Final independent review passed after a reproduced pending-publication recovery fix. An isolated browser mounted the local 1.1.2 schema in the actual MoviePilot FormRender/Vuetify: five storage modes and five schedule modes passed at 1440/390px, values were retained, and no page errors occurred. All management API requests were intercepted; no config was saved and no cloud request was sent. Published/deployed Jellyfin behavior remains outside this local implementation run.
