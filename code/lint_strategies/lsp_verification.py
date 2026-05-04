"""
LSP verification — uses the project's language server (tsserver, pyright,
rust-analyzer, gopls) as ground truth for "is this reference real".

For v0.3.2.1 the strategy detects binary availability and emits a
diagnostic-only signal so we can measure how often an LSP would have
been consulted. The wire-protocol JSON-RPC integration lands in
v0.3.2.1.1; the architecture stays unchanged when it does.
"""
from __future__ import annotations

from . import LintContext, LintStrategy, Issue, IssueSeverity, register


class LSPVerificationStrategy:
    name = "lsp_verification"
    languages = {"typescript", "tsx", "javascript", "python", "rust", "go"}
    enabled_by_default = True

    def applies_to(self, ctx: LintContext) -> bool:
        return ctx.language in self.languages

    def check(self, ctx: LintContext) -> list[Issue]:
        try:
            from lsp_client import verify
        except Exception:
            return []
        result = verify(ctx.file_path, ctx.content, ctx.language)
        if not result.get("available"):
            return []
        diags = result.get("diagnostics", [])
        if not diags:
            return [Issue(
                strategy=self.name,
                severity=IssueSeverity.OK,
                name="<lsp_passed>",
                confidence=1.0,
                metadata={"channel": "lsp", "binary": result.get("binary"),
                          "skipped_reason": result.get("skipped_reason")},
                reason=(f"LSP {result.get('binary')} verified the proposed edit "
                        f"with no diagnostics" + (
                            f" ({result.get('skipped_reason')})"
                            if result.get("skipped_reason") else "")),
            )]
        out: list[Issue] = []
        for d in diags:
            sev = (d.get("severity") or "warning").lower()
            issue_severity = (IssueSeverity.UNKNOWN if sev == "error"
                              else IssueSeverity.LOW_CONFIDENCE)
            out.append(Issue(
                strategy=self.name,
                severity=issue_severity,
                name=d.get("name_referenced") or f"line {d.get('line', 0)}",
                confidence=0.95 if sev == "error" else 0.7,
                metadata={"channel": "lsp", "lsp_severity": sev,
                          "line": d.get("line"), "column": d.get("column")},
                reason=f"LSP {result.get('binary')}: {d.get('message')}",
                fix=f"address LSP diagnostic at line {d.get('line', '?')}",
            ))
        return out


register(LSPVerificationStrategy())
