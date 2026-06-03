# /continue apostrophe audit

Context: `Clarksons.Farm.S05E01.1080p.HEVC.x265-MeGusta[EZTVx.to].mkv`
is indexed by Plex as `Clarkson's Farm`.

Current findings:

- Plex polling was fixed to find series by exact episode file path.
- `/continue` builds candidates from Plex shows plus download history, not from DS
  files directly.
- Without useful history or known totals, Plex-only season 5 with 4 episodes is
  not enough for `/continue` to show a candidate.
- With history using canonical `Clarkson's Farm`, `/continue` can show a
  candidate such as `Plex: 4 из 8`.
- With history or DS titles using `Clarksons Farm`, matching now works against
  Plex title `Clarkson's Farm`.
- Plex-found history for found seasons now stores canonical series fields
  (`kind=series`, `series_query`, `season`) even when the match came from an
  episode filename fallback.
- `/continue` uses TMDB via Plex external ids to get exact season episode
  totals when `TMDB_API_TOKEN` is configured.
- Series bulk active-task detection and planner title matching now use the
  same apostrophe-insensitive title key.
- General Plex movie/show cache lookup now falls back to the same title key
  for possessive apostrophe variants.

Real apostrophe-sensitive spots found:

- `series_continue._history_entry_matches_show`: history title fields vs Plex
  `title` / `original_title`.
- `bot._series_continue_task_matches_candidate`: active DS task title vs
  `/continue` candidate title.
- `bot._series_bulk_downloading_seasons`: series bulk active-task detection.
- `series_bulk_planner._title_matches_series`: series title vs tracker result
  title.
- `bot._plex_show_find` and `_plex_library_find`: canonical Plex title lookup.

Done:

- `/continue` history matching and active DS-task matching
  treat `Clarksons Farm`, `Clarkson's Farm`, `Schitts Creek`, and
  `Schitt's Creek` as the same title key.
- Plex-found season history is enriched with canonical series fields.
- TMDB season totals are used as `known_totals_by_show` for `/continue`.
- Series bulk and general Plex lookup apostrophe matching are fixed.
