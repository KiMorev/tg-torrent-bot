# Series Search & Subscriptions Roadmap

Living checklist for series-related search, subscription, and notification work.
Update this file when a task is completed or reprioritized.

Last updated: 2026-05-24 - kept manual season picker available without Kinopoisk

## P0 - Required Fixes

- [x] Rutracker notify-only subscriptions must not claim that a torrent was added to Download Station.
- [x] Rutracker subscription downloads must treat an empty Download Station task id as a failed add.
- [x] Admin subscription mode toggle must update `notify_policy`/`download_policy` semantics, not only legacy `notify_mode`.
- [x] Advanced subscription picker must block combinations that do nothing, such as silent notifications plus notify-only download.

## P1 - Important Next

- [x] Preserve search render context when returning from the subscription picker to results.
- [x] Escape series/subscription titles in HTML-formatted Telegram messages.
- [x] Always offer manual season selection in "other season" flow, even when Kinopoisk is unavailable.
- [ ] Improve English/no-slash series title cleanup for `Sxx`, `SxxEyy`, quality tags, and release suffixes.
- [ ] Remove normal Jackett subscriptions consistently when a season completes.
- [ ] Keep silent subscription state complete: title, total episodes, completion/removal state.

## P2 - UX And Reliability

- [ ] Broaden episode parser for single-episode formats like `S02E08`, `Серия 8 из 10`, and `E08/10`.
- [ ] Add back/cancel buttons to manual season input.
- [ ] Avoid chat clutter after manual season input by editing or deleting the prompt.
- [ ] Show available seasons when a selected season/quality has zero results.
- [ ] Allow subscribing from complete season releases where that makes product sense.
- [ ] Use a series-aware grouping key for search clusters.
- [ ] Normalize search/subscription error screens around retry/back/close actions.
- [ ] Show policy and progress more clearly in `/subs`.

## Future

- [ ] Add per-subscription edit UI.
- [ ] Split "watch this topic" from "track this series/future seasons".
- [ ] Introduce a normalized `SeriesRelease` model shared by search, Plex, subscriptions, and task metadata.
- [ ] Add series-specific ranking preferences.
- [ ] Remember a user's preferred subscription preset.
- [ ] Add subscription diagnostics: last candidate, parse result, policy decision, download outcome.
- [ ] Surface tracker health per subscription.
