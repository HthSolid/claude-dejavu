---
description: Show recent dejavu plugin error-log records (internal failures, not Bash failures)
argument-hint: [--limit=N] [--component=PREFIX] [--level=error|warning|info] [--with-traceback]
---

Use the claude-dejavu plugin's centralized error log to inspect
plugin-internal failures. The log lives at:

  Linux:   $XDG_DATA_HOME/claude-dejavu/errors.log
  macOS:   ~/Library/Application Support/claude-dejavu/errors.log
  Windows: %APPDATA%\\claude-dejavu\\errors.log

One JSON record per line, auto-rotated at 5 MB (last 3 generations
kept). Each record carries: timestamp, level, component, message,
context, optional traceback, dejavu version.

Use this to:
  - Diagnose why a previous dejavu invocation behaved oddly
  - Forward a clean trace when filing an issue against the plugin
  - Audit which subsystems (indexer.env, mcp.server, rewriter.cascade,
    etc.) have been hitting errors recently

Sample:

  /dejavu-logs                              # last 30 records
  /dejavu-logs --component=indexer.env     # only env-indexer errors
  /dejavu-logs --level=error                # only ERROR-level
  /dejavu-logs --with-traceback             # include Python tracebacks

This is for the PLUGIN's internal log. To see Bash command failures
recorded across past sessions, use `/dejavu-errors` instead.

Args: $ARGUMENTS
