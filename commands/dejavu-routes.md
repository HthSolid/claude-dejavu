---
description: Resolve, list, or look up HTTP routes in the project's API index (Express / FastAPI / Next.js / NestJS / Flask / Django / Hono / Fastify / Koa / OpenAPI)
argument-hint: <method> <path>  |  --list  |  --framework=NAME
---

Use the `dejavu_resolve_route` MCP tool from the claude-dejavu plugin
to verify a URL + method against the project's actual API surface.

Args: $ARGUMENTS

Behavior:
  - With a method + path: resolve in three layers (exact, pattern,
    fuzzy) and surface the best matches with framework / source file /
    line.
  - With `--list` (or no args): use `dejavu_routes` to enumerate every
    declared route in the project.

USE THIS BEFORE writing any client-side call (`fetch`, `axios`,
`useQuery`, `requests.get`, etc.). If the route doesn't resolve, the
URL is fabricated — surface the unknown to the user and suggest
`dejavu_resolve_semantic("create user endpoint")` if they're searching
by intent.

OpenAPI/Swagger files are treated as ground truth when present. Code
declarations (Express `app.get(...)`, FastAPI `@app.get(...)`, etc.)
fill in routes the OpenAPI doesn't cover.

Path normalization handles cross-framework styles automatically:
`:id` ↔ `{id}` ↔ `[id]` all collapse to the canonical `:id`. A concrete
URL like `/api/users/123` correctly matches the parameterized
`/api/users/:id`.
