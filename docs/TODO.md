# TODO — Anilist-Link

Small work that has no issue yet. This file lives at `docs/TODO.md`.

Anything a user would ask for goes to GitHub instead:

- A feature somebody wants → a feature request issue
- A defect → a bug report issue
- A release worth planning → the roadmap

This file takes the rest: the note-to-self, the half-finished edge case, the bracketed
placeholder from scaffolding. It may be untidy. It must not hold anything a user needs,
because a user never opens it.

## Now

Nothing open.

## Next

- [ ] Settings page blocks on Plex and Jellyfin during the server render.
      `src/Web/Routes/Settings.py` calls `get_libraries()` with a 5-second timeout as a
      workaround. Fetch the libraries from the browser after the page loads, the same
      way the download manager fetches Sonarr options
- [ ] AniList token auto-refresh is not wired. Nothing in `src/` stores or uses a
      refresh token or an expiry
- [ ] Plex webhook handler for real-time watch sync. Plex watch sync polls only.
      Jellyfin has `POST /jellyfin/webhook`, and Plex has no equivalent

## Someday

- [ ] Write staff and credits to Plex (deferred from the metadata pillar)
- [ ] GUID-based matching for high-confidence Plex matches (deferred from the metadata
      pillar)

## Scaffold gaps

Bracketed placeholders from `new-project`. Delete a line when its document is complete.
Delete this whole section when the list is empty.

```bash
grep -rn "\[[a-z]" README.md docs/*.md
```

- [ ] None known after the standards retrofit. Run the grep above to confirm.
