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
- With history or DS titles using `Clarksons Farm`, matching fails against
  Plex title `Clarkson's Farm`.

Real apostrophe-sensitive spots found:

- `series_continue._history_entry_matches_show`: history title fields vs Plex
  `title` / `original_title`.
- `bot._series_continue_task_matches_candidate`: active DS task title vs
  `/continue` candidate title.
- `bot._series_bulk_downloading_seasons`: series bulk active-task detection.
- `series_bulk_planner._title_matches_series`: series title vs tracker result
  title.
- `bot._plex_show_find` and `_plex_library_find`: canonical Plex title lookup.

Next scoped fix:

- First fix only `/continue`: make history matching and active DS-task matching
  treat `Clarksons Farm`, `Clarkson's Farm`, `Schitts Creek`, and
  `Schitt's Creek` as the same title key.
- Leave series bulk and general Plex lookup for separate follow-up steps.
