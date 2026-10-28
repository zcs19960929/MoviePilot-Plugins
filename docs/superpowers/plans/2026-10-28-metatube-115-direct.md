# MetaTube 115 Direct Implementation Plan

> Implement the user-approved 1.1.0 design in this checkout, using test-first changes and a final independent review.

**Goal:** Run the MoviePilot plugin without 115Lite: browse 115 with a Cookie, identify films with MetaTube, rename videos/subtitles together, publish a local library, and resolve stable STRM URLs with 302 redirects.

**Architecture:** `cloud115.py` owns the lazy SDK adapter and request control; `direct.py` owns naming, SQLite state, staging and playback resolution; the existing plugin owns host configuration, UI and routes. V2/V3 carry identical implementations.

**Constraints:** Cookie stays in host configuration; no cloud moves/deletes or QR login; no overwriting unowned files; no video proxy; preserve old modes and MetaTube authentication. SDK pins: p115client 0.0.9.6.5.1 and python-concurrenttools 0.1.8 on Python >=3.12.

## Execution

- [x] Add failing tests for folder pagination, direct processing, subtitle matching, recovery and playback.
- [x] Implement the SDK adapter, registry and direct processing behind dependency injection.
- [x] Integrate configuration, browse/check/run/playback APIs and UI into V3, then mirror V2.
- [x] Verify on Python 3.12/3.13, retain legacy tests on Python 3.9, and test with the real pinned SDK in an isolated environment.
- [x] Update README and version manifests to 1.1.0; run an independent review and resolve findings.

## Review focus

- Partial cloud renames must resume using file IDs and recorded subtitle associations.
- An existing NFO must not hide missing STRM/subtitle/image output.
- Account changes, unknown playback identifiers and UA-dependent links must not cross authorization or cache boundaries.
- Existing local files and cloud name collisions must be preserved.
- Direct mode must work with no 115Lite installation and retain old configuration behavior.

## Verification boundary

No real 115 Cookie was supplied. Real NAS rename/subtitle/playback acceptance requires a user-configured test directory and remains separate from offline and pinned-SDK verification.

## Results

- Python 3.12 isolated environment with the real pinned SDK, requests and FastAPI: 36 tests passed, no skips. SDK network responses are simulated; HTTP GET/HEAD routes use a real FastAPI TestClient.
- Python 3.9 and 3.13: 34 tests passed; the 2 SDK/HTTP tests skip because those optional dependencies are absent in these interpreters.
- Python syntax, Node syntax checks of all form scripts, V2/V3 file parity, version/manifest consistency and `git diff --check` passed.
- The independent review's five findings were reproduced and fixed with regressions: account reload race, SDK risk classification, newly arriving subtitles, forced metadata refresh and parent-directory replacement during staging. Full file info now uses the SDK's `fs_file` rather than the parent-less skim endpoint.
- The pinned third-party SDK emits an upstream file ResourceWarning and urllib3-future teardown diagnostic in the isolated process; the current Starlette TestClient also emits an httpx deprecation warning. All test process exit codes are zero. Vendor packages were not patched.
- GitHub publication follows explicit user instruction. No deployment, real cloud mutation or real player acceptance was performed for 1.1.0.
