---
description: Check or browse GraphQL schema (SDL files + inline gql template literals) — catches typos and fabricated fields in queries / mutations
argument-hint: <Type.field>  |  <Type>  |  --types  |  --fields=Type
---

Use the claude-dejavu plugin to either:

1. **Check before referencing a Type.field** — `dejavu_check_gql_field(target)`.
   USE THIS BEFORE composing `gql\`query|mutation { … }\`` operations.

   - ✓ exact — type + field both exist; safe.
   - ≈ cross-type — field exists but on a different type. Either change the parent or add the field.
   - ~ fuzzy — close match (typo); rename to candidate.
   - no match — fabricated.

2. **Browse the type surface** — `dejavu_gql_types(kind?, source?)`.
   Survey every type declared across `*.graphql` SDL files and inline
   `gql\`type X { … }\`` template literals.

3. **Browse fields on one type** — `dejavu_gql_fields(type_name)`.
   Confirm the exact selection-set surface before writing a query.

Args: $ARGUMENTS

This is the GraphQL companion to `/dejavu-routes`, `/dejavu-pages`,
`/dejavu-env`, and `/dejavu-db`. Together they ground every name Claude
generates against the project's actual surface.
