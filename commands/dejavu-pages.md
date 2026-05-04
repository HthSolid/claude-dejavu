---
description: Check or list frontend page routes (Next.js / Remix / SvelteKit / Astro / React Router / Vue Router) — catches duplicate-page creation
argument-hint: <proposed-path>  |  --list  [--framework=NAME] [--kind=page|layout|...]
---

Use the claude-dejavu plugin to either:

1. **Check before creating a new page** — `dejavu_check_new_page(proposed_path)`.
   USE THIS BEFORE writing a new file under a routing-aware directory
   (`app/`, `pages/`, `src/routes/`, `src/pages/`, `app/routes/`, etc.).
   Returns existing pages whose URL would conflict.

   - Exact path match → don't create the new file; EDIT the existing one.
   - Pattern match (`/blog/foo` overlaps with `/blog/:slug`) → use the
     existing parameterized route, or pick a non-overlapping path.
   - Fuzzy near-name match → verify whether the existing page covers the
     same goal; if so, edit there instead.

2. **List the routing surface** — `dejavu_pages(framework?, kind?)`.
   Use to survey what pages exist before proposing a new one, or to find
   which file handles a known URL.

Args: $ARGUMENTS

This is the duplicate-prevention companion to `/dejavu-routes` (which
covers HTTP API endpoints). Together they ensure Claude doesn't
recreate work that already exists for the same goal.
