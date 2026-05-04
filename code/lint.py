#!/usr/bin/env python3
"""
claude-dejavu — proposed-edit linter (v0.3.1).

Parses proposed file content, extracts external references (function calls,
class instantiations, type annotations), then resolves each against the
symbol index. Returns:

  - resolved      : references confirmed to exist in the index
  - unknown       : references that resolve to nothing — likely fabricated
  - low_confidence: references that only resolve via fuzzy match (probable
                    typo or wrong name)

Used by:
  - `dejavu_lint_proposed_edit` MCP tool
  - `claude-dejavu lint` CLI
  - `hooks/pre_tool_use.py` (PreToolUse hook on Edit/Write/MultiEdit)

Scope (v0.3.1):
  - Python: call expressions, class instantiations, type annotations.
  - TypeScript / JavaScript: call expressions, new-expressions, type
    references, import-name bindings.

Out of scope for v0.3.1:
  - Member access on typed objects (`obj.foo` requires type inference).
  - Decorator references.
  - JSX component references.
  - Other languages (Go/Rust/Java/Ruby/Bash) — added in v0.3.1.x.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from symbol_indexer import (
    EXT_TO_LANG,
    EXT_TO_PARSER_LANG,
    _TS_PARSERS,
    _ts_decode,
    resolve_symbol,
)


# ─── Builtins / stdlib allowlists ────────────────────────────────────────────
# Names we never flag as "unknown" because they're language builtins or
# stdlib. Conservative: better to skip a real check than annoy users.

_PY_BUILTINS = frozenset({
    # functions
    "print","len","range","enumerate","zip","map","filter","sorted","reversed",
    "min","max","sum","abs","round","any","all","next","iter",
    "open","input","format","repr","bool","int","float","str","bytes","list",
    "tuple","set","frozenset","dict","type","isinstance","issubclass","hasattr",
    "getattr","setattr","delattr","callable","id","hash","vars","dir","help",
    # exceptions
    "Exception","ValueError","TypeError","KeyError","IndexError","AttributeError",
    "RuntimeError","StopIteration","NotImplementedError","FileNotFoundError",
    "OSError","IOError","ImportError","ModuleNotFoundError","ZeroDivisionError",
    # async / contextlib / typing essentials
    "super","property","classmethod","staticmethod","object","None","True","False",
    "NotImplemented","Ellipsis","__import__",
    # ─── Common method names skipped to reduce false positives ───────────
    "append","extend","insert","remove","pop","clear","copy","count","index",
    "sort","reverse","keys","values","items","get","update","setdefault",
    "join","split","strip","lstrip","rstrip","replace","find","rfind","startswith",
    "endswith","upper","lower","title","capitalize","format","encode","decode",
    "read","readline","readlines","write","writelines","close","flush","seek","tell",
    "add","discard","union","intersection","difference","symmetric_difference",
    "issubset","issuperset","isdisjoint","fromkeys",
    # ─── v0.3.10: Python ecosystem methods ──────────────────────────────
    # FastAPI / Flask / Django common idioms
    "FastAPI","APIRouter","Depends","Body","Query","Path","Header","Cookie",
    "BackgroundTasks","HTTPException","Request","Response","WebSocket",
    "Flask","Blueprint","jsonify","render_template","redirect","url_for",
    "Django","models","Model","CharField","IntegerField","ForeignKey",
    # SQLAlchemy
    "Column","Integer","String","DateTime","Float","Boolean","ForeignKey",
    "relationship","backref","Session","sessionmaker","declarative_base",
    # Pydantic
    "BaseModel","Field","validator","root_validator","ConfigDict",
    # Test frameworks
    "pytest","mark","fixture","raises","approx","skip","skipif","xfail",
    "parametrize","monkeypatch","capsys","tmp_path","caplog",
    # asyncio / concurrent
    "async","await","asyncio","run","gather","wait","create_task","sleep",
    "ensure_future","Lock","Semaphore","Queue","Event","Condition",
    # Type hints
    "Optional","List","Dict","Tuple","Set","Union","Any","Callable",
    "Iterator","Iterable","Generator","Awaitable","Coroutine","Type","TypeVar",
    "Generic","Literal","Final","ClassVar","Annotated","Protocol",
    # Common libs
    "numpy","np","pandas","pd","torch","tf","sklearn","scipy",
    "os","sys","json","re","math","time","random","datetime","collections",
    "functools","itertools","pathlib","Path","logging","logger",
})

_TS_BUILTINS = frozenset({
    # JS globals
    "console","Math","Date","JSON","Object","Array","String","Number","Boolean",
    "RegExp","Map","Set","WeakMap","WeakSet","Symbol","Error","TypeError",
    "RangeError","SyntaxError","ReferenceError","Promise","Proxy","Reflect",
    "globalThis","undefined","null","NaN","Infinity",
    # browser
    "window","document","navigator","location","history","fetch","setTimeout",
    "clearTimeout","setInterval","clearInterval","requestAnimationFrame",
    "alert","confirm","prompt","localStorage","sessionStorage",
    # node
    "process","Buffer","require","module","exports","__dirname","__filename",
    "global","setImmediate","clearImmediate",
    # TS lib basics
    "Partial","Required","Readonly","Record","Pick","Omit","Exclude","Extract",
    "NonNullable","Parameters","ReturnType","InstanceType","Awaited","ThisType",
    "Promise","Iterable","Iterator","Generator","AsyncIterable","Array",
    "ReadonlyArray","Map","Set","WeakMap","WeakSet","Date","RegExp",
    # Common DOM/web types
    "HTMLElement","HTMLInputElement","Event","MouseEvent","KeyboardEvent",
    "Response","Request","Headers","URL","URLSearchParams","FormData","Blob",
    "File","FileList","ReadableStream","WritableStream","TransformStream",
    # ─── Common method names invoked on builtins ─────────────────────────
    # These are skipped because we can't tell from `obj.method()` whether
    # `method` is a builtin method (likely) or a user-defined function with
    # the same name. Erring toward "skip" reduces noise; the real risk
    # is the *callee* not the method-on-known-object.
    "log","info","warn","error","debug","trace","dir","table","group","groupEnd",
    "forEach","map","filter","reduce","find","findIndex","some","every","includes",
    "indexOf","lastIndexOf","slice","splice","push","pop","shift","unshift",
    "concat","join","reverse","sort","flat","flatMap","fill","copyWithin",
    "keys","values","entries","fromEntries","assign","freeze","isFrozen",
    "getOwnPropertyNames","defineProperty","create","getPrototypeOf",
    "then","catch","finally","resolve","reject","all","allSettled","race","any",
    "toString","valueOf","hasOwnProperty","toJSON","propertyIsEnumerable",
    "test","exec","match","matchAll","replace","replaceAll","search","split",
    "trim","trimStart","trimEnd","padStart","padEnd","repeat","startsWith",
    "endsWith","charAt","charCodeAt","codePointAt","fromCharCode","fromCodePoint",
    "stringify","parse","now","getTime","setTime",
    "addEventListener","removeEventListener","dispatchEvent","appendChild",
    "removeChild","replaceChild","cloneNode","setAttribute","getAttribute",
    "querySelector","querySelectorAll","getElementById","getElementsByClassName",
    "getElementsByTagName","createElement","createTextNode","getContext",
    # ─── String / Array / Object instance methods commonly chained ──────
    "substring","substr","toLowerCase","toUpperCase","localeCompare",
    "normalize","at","isArray","of","copyWithin","find","findLast","findLastIndex",
    "flat","flatMap","group","groupBy","toReversed","toSorted","toSpliced","with",
    "isInteger","isFinite","isNaN","isSafeInteger","parseInt","parseFloat",
    "fromCharCode","fromCodePoint","raw","abs","ceil","floor","round","trunc",
    "sign","sqrt","cbrt","pow","exp","log","log2","log10","random",
    "min","max","hypot","atan","atan2","cos","sin","tan","acos","asin",
    "Number","String","Array","Object","Boolean","Symbol","BigInt",
    # JSON / Promise / iteration that show up at every call site
    "stringify","parse","then","catch","finally","resolve","reject",
    "all","allSettled","race","any","next","return","throw",
    "Symbol.iterator","Symbol.asyncIterator","done","value",
    # ─── v0.3.10: library / framework method allowlist ──────────────────
    # Caught from real-world v0.3.7+ noise: the symbol_grounding strategy
    # was flagging Prisma/Drizzle/GraphQL/React/Next.js/test-framework
    # methods as "fabricated" because they're never declared in the
    # project — they live in node_modules. The db_grounding /
    # graphql_grounding / route_grounding strategies handle these
    # correctly at the call-site level; symbol_grounding should ignore
    # them as pure noise.
    # Prisma client (already covered indirectly by db_grounding, but
    # symbol_grounding sees them as bare references).
    "findUnique","findFirst","findMany","create","createMany","update",
    "updateMany","upsert","delete","deleteMany","count","aggregate",
    "groupBy","prisma","tx","trx",
    # Drizzle ORM
    "select","from","insert","values","set","where","leftJoin","rightJoin",
    "innerJoin","fullJoin","groupBy","having","orderBy","limit","offset",
    "returning","onConflictDoNothing","onConflictDoUpdate","db",
    # GraphQL tag
    "gql","graphql",
    # React + React Router
    "useState","useEffect","useMemo","useCallback","useRef","useContext",
    "useReducer","useLayoutEffect","useImperativeHandle","useDebugValue",
    "useId","useTransition","useDeferredValue","useSyncExternalStore",
    "useInsertionEffect","useNavigate","useLocation","useParams",
    "useSearchParams","useRoutes","useMatch","useResolvedPath",
    "useNavigationType","useOutletContext","useOutlet","useLoaderData",
    "useActionData","useSubmit","useFetcher","useFormAction","Link",
    "NavLink","Route","Routes","BrowserRouter","Navigate","Outlet",
    # Next.js
    "useRouter","useSearchParams","usePathname","redirect","notFound",
    "headers","cookies","draftMode","revalidatePath","revalidateTag",
    "Image","Link","Script","Head",
    # Test frameworks (Jest / Vitest / Mocha)
    "describe","it","test","expect","beforeAll","beforeEach","afterAll",
    "afterEach","jest","vi","vitest","mock","spy","spyOn","fn",
    "toBe","toEqual","toBeTruthy","toBeFalsy","toContain","toHaveBeenCalled",
    "toHaveBeenCalledWith","toThrow","toMatch","toMatchObject","toMatchSnapshot",
    "toBeDefined","toBeUndefined","toBeNull","toBeGreaterThan","toBeLessThan",
    "rejects","resolves","not","todo","skip","only","each","concurrent",
    # Express / Fastify / Koa / Hono / NestJS
    "express","fastify","koa","hono","app","router","get","post","put",
    "patch","delete","head","options","use","listen","static","json",
    "urlencoded","cookieParser","cors","helmet","morgan","compression",
    # Async / promise utilities
    "async","await","yield","Promise","Generator","AsyncIterator",
    # Common util libs
    "lodash","_","ramda","R","date_fns","format","parseISO","addDays",
    "subDays","startOfDay","endOfDay","isSameDay","differenceInDays",
    # Logger / observability
    "logger","log","trace","span","metric","gauge","counter","histogram",
    # Feature-flag SDK methods (LaunchDarkly / Statsig / PostHog / GrowthBook / Unleash)
    "variation","variationDetail","isEnabled","isFeatureEnabled","checkGate",
    "getConfig","getFeatureGate","getFeatureFlag","feature","evalFeature",
    "isOn","isOff","getFeatureValue","client","Statsig","posthog","gb",
    "growthbook","unleash","launchdarkly","ldClient",
})


# ─── Reference extractors ────────────────────────────────────────────────────


def _extract_python_refs(source: str):
    """Extract (defined_names, referenced_names, var_types, member_calls)
    from Python source via tree-sitter.

    Defined: top-level functions, classes, methods, module-level
             assignments, function parameters, local assignments.
    Referenced: callees of call expressions, type identifiers in annotations,
                class names in instantiations and bases.
    Var_types: best-effort {var_name: type_name} from explicit annotations
               and `var = TypeName(...)` constructor patterns. Used by
               lint to ground `obj.method()` against the receiver type.
    """
    parser = _TS_PARSERS.get("python")
    if not parser:
        return set(), set(), {}, [], {}, []
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)

    defined: set[str] = set()
    referenced: set[str] = set()
    imported: set[str] = set()
    var_types: dict[str, str] = {}
    member_calls: list[tuple[str, str]] = []

    def walk(node):
        t = node.type
        # ─── Definitions ─────────────────────────────────────────────────
        if t in ("function_definition", "async_function_definition", "class_definition"):
            name_node = node.child_by_field_name("name")
            if name_node:
                defined.add(_ts_decode(name_node, src_b))
            params = node.child_by_field_name("parameters")
            if params:
                for p in params.named_children:
                    if p.type == "identifier":
                        defined.add(_ts_decode(p, src_b))
                    elif p.type in ("typed_parameter", "default_parameter", "typed_default_parameter"):
                        for c in p.named_children:
                            if c.type == "identifier":
                                defined.add(_ts_decode(c, src_b))
                                break
        elif t == "assignment":
            left = node.child_by_field_name("left")
            if left and left.type == "identifier":
                var_name = _ts_decode(left, src_b)
                defined.add(var_name)
                # Infer type from annotation: `foo: Bar = ...`
                ty = node.child_by_field_name("type")
                if ty:
                    for tn in _iter_identifiers(ty, src_b):
                        var_types[var_name] = tn; break
                else:
                    # Infer type from `foo = TypeName(...)` constructor call
                    right = node.child_by_field_name("right")
                    if right and right.type == "call":
                        fn = right.child_by_field_name("function")
                        if fn and fn.type == "identifier":
                            ctor = _ts_decode(fn, src_b)
                            # Heuristic: PascalCase callee = likely class constructor
                            if ctor and ctor[0].isupper():
                                var_types[var_name] = ctor
        elif t == "import_statement":
            # `import foo` or `import foo, bar` or `import foo as f`
            for c in node.named_children:
                if c.type == "dotted_name":
                    last = c.named_children[-1] if c.named_children else c
                    imported.add(_ts_decode(last, src_b))
                elif c.type == "aliased_import":
                    alias = c.child_by_field_name("alias")
                    if alias:
                        imported.add(_ts_decode(alias, src_b))
                    else:
                        nm = c.child_by_field_name("name")
                        if nm:
                            imported.add(_ts_decode(nm, src_b))
        elif t == "import_from_statement":
            # `from X import foo, bar`
            for c in node.named_children:
                if c.type in ("dotted_name", "identifier"):
                    pass  # module name itself
            # capture imported names (siblings after the module)
            module = node.child_by_field_name("module_name")
            for c in node.named_children:
                if c is module:
                    continue
                if c.type == "dotted_name" and c.named_children:
                    imported.add(_ts_decode(c.named_children[-1], src_b))
                elif c.type == "aliased_import":
                    alias = c.child_by_field_name("alias")
                    nm = c.child_by_field_name("name")
                    target = alias or nm
                    if target:
                        imported.add(_ts_decode(target, src_b))
                elif c.type == "identifier":
                    imported.add(_ts_decode(c, src_b))
        # ─── References ──────────────────────────────────────────────────
        elif t == "call":
            fn = node.child_by_field_name("function")
            if fn is not None:
                if fn.type == "identifier":
                    referenced.add(_ts_decode(fn, src_b))
                elif fn.type == "attribute":
                    # obj.method(...) — capture both obj and method names,
                    # plus the (obj, method) pair for type-aware lookup.
                    obj = fn.child_by_field_name("object")
                    attr = fn.child_by_field_name("attribute")
                    if obj and obj.type == "identifier":
                        recv_name = _ts_decode(obj, src_b)
                        referenced.add(recv_name)
                        if attr:
                            method_name = _ts_decode(attr, src_b)
                            member_calls.append((recv_name, method_name))
                    if attr:
                        referenced.add(_ts_decode(attr, src_b))
        elif t == "type":
            # Python 3.10+: PEP 695 type alias. Scan children.
            for c in node.named_children:
                walk(c); return  # handled in recursion
        elif t == "argument_list":
            pass

        for c in node.named_children:
            walk(c)

    walk(tree.root_node)
    # Type identifiers in annotations: tree-sitter exposes them as `identifier`
    # inside `type` field of typed_parameter / function_definition return_type.
    # Re-walk specifically for those.
    def walk_types(node):
        if node.type in ("typed_parameter", "typed_default_parameter"):
            type_node = node.child_by_field_name("type") or (
                node.named_children[1] if len(node.named_children) > 1 else None
            )
            if type_node:
                for n in _iter_identifiers(type_node, src_b):
                    referenced.add(n)
        if node.type in ("function_definition", "async_function_definition"):
            ret = node.child_by_field_name("return_type")
            if ret:
                for n in _iter_identifiers(ret, src_b):
                    referenced.add(n)
        for c in node.named_children:
            walk_types(c)

    walk_types(tree.root_node)
    referenced -= defined
    referenced -= imported
    return defined, referenced, var_types, member_calls


def _extract_typescript_refs(source: str, lang: str):
    """Extract (defined, referenced, var_types, member_calls) from TS/TSX/JS.

    member_calls: list of (receiver_name, method_name) pairs for `obj.method()`
    invocations. The linter joins this with `var_types` to look up the
    receiver's type and ground the method against `Type.method`.

    Var_types from:
      - explicit annotations: `const foo: Bar = ...`
      - `new` expressions:    `const foo = new Bar(...)`
      - type assertions:      `const foo = X as Bar`
    """
    parser_key = lang if lang in ("typescript", "tsx", "javascript") else "typescript"
    parser = _TS_PARSERS.get(parser_key) or _TS_PARSERS.get("typescript")
    if not parser:
        return set(), set(), {}, [], {}, []
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)

    defined: set[str] = set()
    referenced: set[str] = set()
    imported: set[str] = set()
    var_types: dict[str, str] = {}
    member_calls: list[tuple[str, str]] = []

    def walk(node):
        t = node.type
        # ─── Definitions ─────────────────────────────────────────────────
        if t in ("function_declaration", "generator_function_declaration",
                 "class_declaration", "interface_declaration",
                 "type_alias_declaration", "enum_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node:
                defined.add(_ts_decode(name_node, src_b))
        elif t == "variable_declarator":
            name_node = node.child_by_field_name("name")
            if name_node and name_node.type == "identifier":
                var_name = _ts_decode(name_node, src_b)
                defined.add(var_name)
                # Type from explicit annotation: `const foo: Bar = ...`
                ty = node.child_by_field_name("type")
                if ty:
                    for tn in _iter_identifiers(ty, src_b):
                        var_types[var_name] = tn; break
                else:
                    # Type from RHS new-expression: `const foo = new Bar(...)`
                    val = node.child_by_field_name("value")
                    if val and val.type == "new_expression":
                        ctor = val.child_by_field_name("constructor")
                        if ctor and ctor.type == "identifier":
                            var_types[var_name] = _ts_decode(ctor, src_b)
                    elif val and val.type == "as_expression":
                        # `const foo = bar as Foo`
                        ty_node = val.child_by_field_name("type")
                        if ty_node:
                            for tn in _iter_identifiers(ty_node, src_b):
                                var_types[var_name] = tn; break
        elif t == "method_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                defined.add(_ts_decode(name_node, src_b))
        elif t == "formal_parameters":
            for p in node.named_children:
                if p.type in ("required_parameter", "optional_parameter"):
                    pat = p.child_by_field_name("pattern")
                    if pat and pat.type == "identifier":
                        defined.add(_ts_decode(pat, src_b))
                elif p.type == "identifier":
                    defined.add(_ts_decode(p, src_b))
        # ─── Imports ─────────────────────────────────────────────────────
        elif t == "import_statement":
            # import { foo, bar as baz } from 'x'
            # import default from 'x'
            # import * as ns from 'x'
            for c in node.named_children:
                if c.type == "import_clause":
                    for cc in c.named_children:
                        if cc.type == "identifier":  # default import
                            imported.add(_ts_decode(cc, src_b))
                        elif cc.type == "named_imports":
                            for spec in cc.named_children:
                                if spec.type == "import_specifier":
                                    alias = spec.child_by_field_name("alias")
                                    nm = spec.child_by_field_name("name")
                                    target = alias or nm
                                    if target:
                                        imported.add(_ts_decode(target, src_b))
                        elif cc.type == "namespace_import":
                            for ccc in cc.named_children:
                                if ccc.type == "identifier":
                                    imported.add(_ts_decode(ccc, src_b))
        # ─── References ──────────────────────────────────────────────────
        elif t == "call_expression":
            fn = node.child_by_field_name("function")
            if fn:
                if fn.type == "identifier":
                    referenced.add(_ts_decode(fn, src_b))
                elif fn.type == "member_expression":
                    obj = fn.child_by_field_name("object")
                    prop = fn.child_by_field_name("property")
                    if obj and obj.type == "identifier":
                        recv_name = _ts_decode(obj, src_b)
                        referenced.add(recv_name)
                        if prop and prop.type == "property_identifier":
                            method_name = _ts_decode(prop, src_b)
                            member_calls.append((recv_name, method_name))
                    if prop and prop.type == "property_identifier":
                        referenced.add(_ts_decode(prop, src_b))
        elif t == "new_expression":
            ctor = node.child_by_field_name("constructor")
            if ctor:
                if ctor.type == "identifier":
                    referenced.add(_ts_decode(ctor, src_b))
                elif ctor.type == "member_expression":
                    prop = ctor.child_by_field_name("property")
                    if prop:
                        referenced.add(_ts_decode(prop, src_b))
        elif t == "type_identifier":
            referenced.add(_ts_decode(node, src_b))
        elif t == "type_reference":
            for c in node.named_children:
                if c.type == "type_identifier":
                    referenced.add(_ts_decode(c, src_b))

        for c in node.named_children:
            walk(c)

    walk(tree.root_node)
    referenced -= defined
    referenced -= imported
    return defined, referenced, var_types, member_calls


# ─── Builtin sets for the additional languages ──────────────────────────────

_GO_BUILTINS = frozenset({
    # builtin functions
    "make","new","len","cap","append","copy","delete","close","panic","recover",
    "print","println","complex","real","imag",
    # types
    "bool","byte","rune","string","int","int8","int16","int32","int64",
    "uint","uint8","uint16","uint32","uint64","uintptr","float32","float64",
    "complex64","complex128","error","any","comparable",
    # values/constants
    "true","false","nil","iota",
    # common stdlib often called bare in tests/snippets — extending could add:
})

_RUST_BUILTINS = frozenset({
    # primitives
    "bool","char","u8","u16","u32","u64","u128","usize","i8","i16","i32","i64",
    "i128","isize","f32","f64","str","String",
    # std prelude (commonly used without explicit import in 2021)
    "Option","Some","None","Result","Ok","Err","Vec","Box","Rc","Arc",
    "Default","Clone","Copy","Send","Sync","Drop","Sized","Iterator","IntoIterator",
    "Display","Debug","Hash","Eq","PartialEq","Ord","PartialOrd",
    "println","print","eprintln","eprint","format","write","writeln",
    "vec","panic","assert","assert_eq","assert_ne","debug_assert",
    "todo","unimplemented","unreachable","matches","include_str","include_bytes",
    # common methods that show up but aren't in the index
    "to_string","to_owned","clone","into","unwrap","expect","map","and_then",
    "or_else","unwrap_or","unwrap_or_else","unwrap_or_default","ok_or","is_some",
    "is_none","is_ok","is_err","as_ref","as_mut","as_deref","iter","iter_mut",
    "into_iter","collect","push","pop","insert","remove","len","is_empty",
    "contains","get","get_mut","clear","extend","drain","split","trim","parse",
    "from","from_str","new","default","with_capacity","capacity","reserve",
})

_JAVA_BUILTINS = frozenset({
    # primitives
    "boolean","byte","short","int","long","float","double","char","void",
    # java.lang
    "Object","String","Integer","Long","Double","Float","Boolean","Character",
    "Byte","Short","Number","Math","System","Thread","Runnable","Throwable",
    "Exception","RuntimeException","NullPointerException","IllegalArgumentException",
    "IllegalStateException","Class","StringBuilder","StringBuffer","Iterable",
    "Iterator","Comparable","Comparator","Cloneable","Enum",
    # java.util
    "List","ArrayList","LinkedList","Map","HashMap","LinkedHashMap","TreeMap",
    "Set","HashSet","LinkedHashSet","TreeSet","Collection","Collections",
    "Arrays","Optional","Objects","Stream","Collectors","Function","Consumer",
    "Predicate","Supplier","BiFunction","BiConsumer","BiPredicate",
    # common method names skipped to reduce noise on chains
    "out","println","print","printf","format","getLogger","info","warn","error",
    "debug","trace","getName","getMessage","getClass","equals","hashCode","toString",
    "size","isEmpty","contains","add","remove","get","set","put","keySet",
    "values","entrySet","stream","collect","map","filter","forEach","reduce",
})

_RUBY_BUILTINS = frozenset({
    # Kernel methods
    "puts","print","p","pp","gets","raise","fail","catch","throw","loop",
    "lambda","proc","require","require_relative","load","autoload",
    "attr_accessor","attr_reader","attr_writer","include","extend","prepend",
    "private","public","protected","module_function",
    # core types
    "Integer","Float","String","Symbol","Array","Hash","Range","Regexp",
    "Proc","Lambda","Object","Module","Class","Comparable","Enumerable",
    "TrueClass","FalseClass","NilClass","Numeric","Method","UnboundMethod",
    # exceptions
    "Exception","StandardError","RuntimeError","ArgumentError","NameError",
    "NoMethodError","TypeError","KeyError","IndexError","NotImplementedError",
    "IOError","FileNotFoundError","ZeroDivisionError",
    # rails-ish common
    "self","new","initialize","to_s","to_a","to_h","to_i","to_f","inspect",
    "send","public_send","define_method","method_missing","respond_to?",
    "is_a?","kind_of?","instance_of?","nil?","empty?","blank?","present?",
    "each","map","select","reject","find","any?","all?","none?","reduce","inject",
})

_BASH_BUILTINS = frozenset({
    # POSIX builtins / shell keywords
    "echo","printf","read","cd","pwd","exit","return","exec","source",
    "export","unset","set","shift","trap","wait","kill","jobs","fg","bg",
    "alias","unalias","hash","type","command","builtin","getopts","let",
    "test","true","false","help","local","declare","typeset","readonly",
    "function","time","times","umask","ulimit","eval","sleep",
    # ubiquitous coreutils that show up bare in scripts
    "ls","cat","grep","sed","awk","cut","sort","uniq","wc","head","tail",
    "tr","find","xargs","tee","mkdir","rmdir","rm","mv","cp","ln","touch",
    "chmod","chown","chgrp","stat","du","df","date","tar","gzip","gunzip",
    "curl","wget","ssh","scp","rsync","git","docker","systemctl","journalctl",
    "ps","top","htop","kill","killall","pgrep","pkill","nohup","pidof",
    "which","whereis","whoami","id","groups","env","printenv","basename",
    "dirname","realpath","readlink","mktemp","yes","seq","sleep","watch",
})


# ─── Reference extractors for the additional languages ─────────────────────


def _extract_go_refs(source: str):
    parser = _TS_PARSERS.get("go")
    if not parser:
        return set(), set(), {}, [], {}, []
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)
    defined: set[str] = set()
    referenced: set[str] = set()
    imported: set[str] = set()

    def walk(node):
        t = node.type
        if t == "function_declaration":
            n = node.child_by_field_name("name")
            if n: defined.add(_ts_decode(n, src_b))
            params = node.child_by_field_name("parameters")
            if params:
                for p in params.named_children:
                    if p.type == "parameter_declaration":
                        for c in p.named_children:
                            if c.type == "identifier":
                                defined.add(_ts_decode(c, src_b))
        elif t == "method_declaration":
            n = node.child_by_field_name("name")
            if n: defined.add(_ts_decode(n, src_b))
        elif t == "type_declaration":
            for spec in node.named_children:
                if spec.type == "type_spec":
                    n = spec.child_by_field_name("name")
                    if n: defined.add(_ts_decode(n, src_b))
        elif t in ("var_declaration", "const_declaration"):
            for spec in node.named_children:
                for c in spec.named_children:
                    if c.type == "identifier":
                        defined.add(_ts_decode(c, src_b))
        elif t == "import_declaration":
            for spec in node.named_children:
                if spec.type == "import_spec":
                    nm = spec.child_by_field_name("name")
                    pth = spec.child_by_field_name("path")
                    if nm:
                        imported.add(_ts_decode(nm, src_b))
                    elif pth:
                        # pkg path → pkg basename is the implicit import name
                        path = _ts_decode(pth, src_b).strip('"').rstrip('"')
                        imported.add(path.rsplit("/", 1)[-1])
        # references
        elif t == "call_expression":
            fn = node.child_by_field_name("function")
            if fn:
                if fn.type == "identifier":
                    referenced.add(_ts_decode(fn, src_b))
                elif fn.type == "selector_expression":
                    operand = fn.child_by_field_name("operand")
                    field = fn.child_by_field_name("field")
                    if operand and operand.type == "identifier":
                        referenced.add(_ts_decode(operand, src_b))
                    if field:
                        referenced.add(_ts_decode(field, src_b))
        elif t == "type_identifier":
            referenced.add(_ts_decode(node, src_b))
        elif t == "composite_literal":
            ty = node.child_by_field_name("type")
            if ty and ty.type == "type_identifier":
                referenced.add(_ts_decode(ty, src_b))
        for c in node.named_children:
            walk(c)

    walk(tree.root_node)
    referenced -= defined
    referenced -= imported
    return defined, referenced, {}, []


def _extract_rust_refs(source: str) -> tuple[set[str], set[str]]:
    parser = _TS_PARSERS.get("rust")
    if not parser:
        return set(), set(), {}, []
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)
    defined: set[str] = set()
    referenced: set[str] = set()
    imported: set[str] = set()

    def collect_use_paths(node):
        # `use foo::bar::Baz;` -> imports {Baz}; `use foo::{a, b};` -> {a,b}
        for c in node.named_children:
            if c.type == "scoped_identifier":
                n = c.child_by_field_name("name")
                if n:
                    imported.add(_ts_decode(n, src_b))
            elif c.type == "scoped_use_list":
                lst = c.child_by_field_name("list")
                if lst:
                    for item in lst.named_children:
                        if item.type == "use_as_clause":
                            alias = item.child_by_field_name("alias")
                            if alias:
                                imported.add(_ts_decode(alias, src_b))
                        elif item.type == "identifier":
                            imported.add(_ts_decode(item, src_b))
                        elif item.type == "scoped_identifier":
                            collect_use_paths(item)
            elif c.type == "use_as_clause":
                alias = c.child_by_field_name("alias")
                if alias: imported.add(_ts_decode(alias, src_b))
            elif c.type == "identifier":
                imported.add(_ts_decode(c, src_b))

    def walk(node):
        t = node.type
        if t == "function_item":
            n = node.child_by_field_name("name")
            if n: defined.add(_ts_decode(n, src_b))
        elif t in ("struct_item", "enum_item", "trait_item", "type_item",
                   "const_item", "static_item"):
            n = node.child_by_field_name("name")
            if n: defined.add(_ts_decode(n, src_b))
        elif t == "let_declaration":
            pat = node.child_by_field_name("pattern")
            if pat and pat.type == "identifier":
                defined.add(_ts_decode(pat, src_b))
        elif t == "use_declaration":
            for c in node.named_children:
                collect_use_paths(c)
        # references
        elif t == "call_expression":
            fn = node.child_by_field_name("function")
            if fn:
                if fn.type == "identifier":
                    referenced.add(_ts_decode(fn, src_b))
                elif fn.type == "scoped_identifier":
                    n = fn.child_by_field_name("name")
                    if n: referenced.add(_ts_decode(n, src_b))
                elif fn.type == "field_expression":
                    f = fn.child_by_field_name("field")
                    if f: referenced.add(_ts_decode(f, src_b))
        elif t == "type_identifier":
            referenced.add(_ts_decode(node, src_b))
        elif t == "macro_invocation":
            mac = node.child_by_field_name("macro")
            if mac and mac.type == "identifier":
                referenced.add(_ts_decode(mac, src_b))
        for c in node.named_children:
            walk(c)

    walk(tree.root_node)
    referenced -= defined
    referenced -= imported
    return defined, referenced, {}, []


def _extract_java_refs(source: str) -> tuple[set[str], set[str]]:
    parser = _TS_PARSERS.get("java")
    if not parser:
        return set(), set(), {}, []
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)
    defined: set[str] = set()
    referenced: set[str] = set()
    imported: set[str] = set()

    def walk(node):
        t = node.type
        if t in ("class_declaration", "interface_declaration", "enum_declaration"):
            n = node.child_by_field_name("name")
            if n: defined.add(_ts_decode(n, src_b))
        elif t == "method_declaration":
            n = node.child_by_field_name("name")
            if n: defined.add(_ts_decode(n, src_b))
        elif t == "import_declaration":
            # Java imports: `import a.b.C;` -> imported {C}
            for c in node.named_children:
                if c.type == "scoped_identifier":
                    nm = c.child_by_field_name("name")
                    if nm: imported.add(_ts_decode(nm, src_b))
                elif c.type == "identifier":
                    imported.add(_ts_decode(c, src_b))
        # references
        elif t == "method_invocation":
            n = node.child_by_field_name("name")
            if n: referenced.add(_ts_decode(n, src_b))
            obj = node.child_by_field_name("object")
            if obj and obj.type == "identifier":
                referenced.add(_ts_decode(obj, src_b))
        elif t == "object_creation_expression":
            ty = node.child_by_field_name("type")
            if ty and ty.type == "type_identifier":
                referenced.add(_ts_decode(ty, src_b))
        elif t == "type_identifier":
            referenced.add(_ts_decode(node, src_b))
        for c in node.named_children:
            walk(c)

    walk(tree.root_node)
    referenced -= defined
    referenced -= imported
    return defined, referenced, {}, []


def _extract_ruby_refs(source: str) -> tuple[set[str], set[str]]:
    parser = _TS_PARSERS.get("ruby")
    if not parser:
        return set(), set(), {}, []
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)
    defined: set[str] = set()
    referenced: set[str] = set()

    def walk(node):
        t = node.type
        if t in ("method", "singleton_method"):
            n = node.child_by_field_name("name")
            if n: defined.add(_ts_decode(n, src_b))
        elif t in ("class", "module"):
            n = node.child_by_field_name("name")
            if n: defined.add(_ts_decode(n, src_b))
        elif t == "assignment":
            left = node.child_by_field_name("left")
            if left and left.type in ("identifier", "constant"):
                defined.add(_ts_decode(left, src_b))
        # references
        elif t == "call":
            recv = node.child_by_field_name("receiver")
            method = node.child_by_field_name("method")
            if method:
                referenced.add(_ts_decode(method, src_b))
            if recv and recv.type == "identifier":
                referenced.add(_ts_decode(recv, src_b))
            if recv and recv.type == "constant":
                referenced.add(_ts_decode(recv, src_b))
        elif t == "constant":
            referenced.add(_ts_decode(node, src_b))
        for c in node.named_children:
            walk(c)

    walk(tree.root_node)
    referenced -= defined
    return defined, referenced, {}, []


def _extract_bash_refs(source: str) -> tuple[set[str], set[str]]:
    parser = _TS_PARSERS.get("bash")
    if not parser:
        return set(), set(), {}, []
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)
    defined: set[str] = set()
    referenced: set[str] = set()

    def walk(node):
        t = node.type
        if t == "function_definition":
            nm = node.child_by_field_name("name")
            if not nm:
                for cc in node.named_children:
                    if cc.type in ("word", "concatenation"):
                        nm = cc; break
            if nm: defined.add(_ts_decode(nm, src_b))
        elif t == "variable_assignment":
            nm = node.child_by_field_name("name")
            if nm: defined.add(_ts_decode(nm, src_b))
        # references: bash treats commands as words. The first word of a
        # command is the program/function being invoked.
        elif t == "command":
            nm = node.child_by_field_name("name")
            if nm and nm.type == "command_name":
                inner = nm.named_children[0] if nm.named_children else nm
                if inner.type == "word":
                    referenced.add(_ts_decode(inner, src_b))
        for c in node.named_children:
            walk(c)

    walk(tree.root_node)
    referenced -= defined
    return defined, referenced, {}, []


def _iter_identifiers(node, src_b: bytes) -> Iterable[str]:
    """Yield every identifier (or type_identifier) under `node`."""
    if node.type in ("identifier", "type_identifier"):
        yield _ts_decode(node, src_b)
    for c in node.named_children:
        yield from _iter_identifiers(c, src_b)


# ─── Public API ──────────────────────────────────────────────────────────────


def lint_proposed_edit(
    content: str,
    file_path: str,
    project_slug: str,
    conn,
    *,
    min_confidence_sim: float = 0.7,
    min_confidence_ed: int = 2,
) -> dict:
    """Parse `content`, extract external references, look up each in the
    symbol index for `project_slug`. Returns a verdict dict:

      {
        "language": "python" | "typescript" | "javascript" | "tsx",
        "resolved": [{name, count}, ...],            # confirmed exact match
        "low_confidence": [                          # only fuzzy match available
          {name, count, top_candidate: {name, sim, ed, file_path:line}}
        ],
        "unknown": [                                 # nothing in the index
          {name, count, top_candidate: {...}|None}
        ],
        "skipped_builtins": [...],                   # informational
        "summary": {
          "total_references": N,
          "resolved": A, "low_confidence": B, "unknown": C,
          "verdict": "ok" | "warn" | "block",
        },
      }

    Verdict rules:
      - "block":  any unknown reference (likely fabricated).
      - "warn":   only low-confidence matches (likely typo).
      - "ok":     all references resolved exactly.
    """
    ext = Path(file_path).suffix.lower()
    lang = EXT_TO_LANG.get(ext)
    if lang is None:
        return {
            "language": None,
            "resolved": [], "low_confidence": [], "unknown": [],
            "skipped_builtins": [],
            "summary": {"total_references": 0, "resolved": 0,
                        "low_confidence": 0, "unknown": 0,
                        "verdict": "ok",
                        "note": f"unsupported_extension:{ext}"},
        }

    parser_lang = EXT_TO_PARSER_LANG.get(ext, lang)

    # ─── v0.4.0: prose languages skip symbol extraction entirely ────
    # The strategy registry still runs (prose_grounding will fire for
    # these). We set empty sentinels so the symbol-grounding pipeline
    # produces zero output, then fall through to the strategies pass.
    _is_prose = parser_lang in ("markdown", "text", "rst", "org", "latex")

    if _is_prose:
        defined: set[str] = set()
        referenced: set[str] = set()
        var_types: dict[str, str] = {}
        member_calls: list[tuple[str, str]] = []
        builtins_set: set[str] = set()
    elif parser_lang == "python":
        defined, referenced, var_types, member_calls = _extract_python_refs(content)
        builtins_set = _PY_BUILTINS
    elif parser_lang in ("typescript", "tsx", "javascript"):
        defined, referenced, var_types, member_calls = _extract_typescript_refs(content, parser_lang)
        builtins_set = _TS_BUILTINS
    elif parser_lang == "go":
        defined, referenced, var_types, member_calls = _extract_go_refs(content)
        builtins_set = _GO_BUILTINS
    elif parser_lang == "rust":
        defined, referenced, var_types, member_calls = _extract_rust_refs(content)
        builtins_set = _RUST_BUILTINS
    elif parser_lang == "java":
        defined, referenced, var_types, member_calls = _extract_java_refs(content)
        builtins_set = _JAVA_BUILTINS
    elif parser_lang == "ruby":
        defined, referenced, var_types, member_calls = _extract_ruby_refs(content)
        builtins_set = _RUBY_BUILTINS
    elif parser_lang == "bash":
        defined, referenced, var_types, member_calls = _extract_bash_refs(content)
        builtins_set = _BASH_BUILTINS
    else:
        return {
            "language": lang,
            "resolved": [], "low_confidence": [], "unknown": [],
            "skipped_builtins": [],
            "summary": {"total_references": 0, "resolved": 0,
                        "low_confidence": 0, "unknown": 0,
                        "verdict": "ok",
                        "note": f"no_lint_parser_for:{lang}"},
        }

    # ─── Type-aware grounding for member calls ─────────────────────────
    # If we have a `(receiver_var, method_name)` pair AND the receiver's type
    # is in our var_types map AND `Type.method_name` exists in the index as
    # a method, we can confidently resolve that without a fuzzy lookup —
    # AND we can SKIP `method_name` from the bare-name reference set so it
    # doesn't get a misleading false-resolve against an unrelated function
    # of the same name elsewhere in the codebase.
    methods_resolved_via_type: dict[str, dict] = {}
    methods_failed_type_check: dict[str, dict] = {}
    for recv, method in member_calls:
        if method in builtins_set:
            continue
        recv_type = var_types.get(recv)
        if not recv_type:
            continue
        # Look for `RecvType.method` in the qualified_name index.
        qn_query = f"{recv_type}.{method}"
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, name, kind, file_path, start_line, qualified_name
                   FROM symbols
                   WHERE project_slug = %s AND qualified_name = %s
                   LIMIT 3""",
                (project_slug, qn_query),
            )
            rows = cur.fetchall()
        if rows:
            r0 = rows[0]
            methods_resolved_via_type[method] = {
                "name": r0[1], "kind": r0[2], "file_path": r0[3],
                "start_line": r0[4], "qualified_name": r0[5],
                "via_receiver_type": recv_type,
            }
        else:
            # Receiver type known but method not found on it — record as
            # fail so we surface it in the unknown list with type context.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM symbols WHERE project_slug=%s AND name=%s LIMIT 1",
                    (project_slug, recv_type),
                )
                receiver_type_exists = cur.fetchone() is not None
            methods_failed_type_check[method] = {
                "receiver_type": recv_type,
                "receiver_type_exists": receiver_type_exists,
            }

    skipped_builtins: list[str] = []
    to_check: list[str] = []
    for name in sorted(referenced):
        if name in builtins_set:
            skipped_builtins.append(name); continue
        # Names starting with underscore are usually private — skip dunders.
        if name.startswith("__") and name.endswith("__"):
            skipped_builtins.append(name); continue
        # If we already resolved this name via a typed receiver, don't
        # double-count it as a bare-name reference.
        if name in methods_resolved_via_type:
            continue
        to_check.append(name)

    resolved: list[dict] = []
    low_confidence: list[dict] = []
    unknown: list[dict] = []

    # Methods resolved via receiver type are high-confidence.
    for method, info in methods_resolved_via_type.items():
        resolved.append({
            "name": method, "count": 1, "kind": info["kind"],
            "file_path": info["file_path"],
            "via_receiver_type": info["via_receiver_type"],
        })

    # Methods that failed the type check go straight to unknown — we know
    # the receiver type and can definitively say the method isn't on it.
    for method, info in methods_failed_type_check.items():
        if method in to_check:
            to_check.remove(method)
        if info["receiver_type_exists"]:
            note = f"method not on receiver type {info['receiver_type']!r}"
        else:
            note = f"receiver type {info['receiver_type']!r} also not in index"
        unknown.append({"name": method, "count": 1,
                        "top_candidate": None,
                        "note": note,
                        "via_receiver_type": info["receiver_type"]})

    for name in to_check:
        # Use exact match first (fuzzy=False is much faster), only fall back
        # to fuzzy if no exact hit.
        exact = resolve_symbol(name, project_slug=project_slug, conn=conn,
                               fuzzy=False, limit=3)
        if exact:
            resolved.append({"name": name, "count": 1,
                             "kind": exact[0]["kind"],
                             "file_path": exact[0]["file_path"]})
            continue
        # Fuzzy candidates
        fuzzy = resolve_symbol(name, project_slug=project_slug, conn=conn,
                               fuzzy=True, limit=3)
        if not fuzzy:
            unknown.append({"name": name, "count": 1, "top_candidate": None})
            continue
        top = fuzzy[0]
        is_high_conf = (
            top["trigram_sim"] >= min_confidence_sim and
            top["edit_distance"] <= min_confidence_ed
        )
        cand_summary = {
            "name": top["name"],
            "kind": top["kind"],
            "trigram_sim": round(top["trigram_sim"], 3),
            "edit_distance": top["edit_distance"],
            "rerank_score": round(top.get("rerank_score", 0.0), 3),
            "file_path": top["file_path"],
            "start_line": top["start_line"],
        }
        if is_high_conf:
            low_confidence.append({"name": name, "count": 1,
                                   "top_candidate": cand_summary})
        else:
            unknown.append({"name": name, "count": 1,
                            "top_candidate": cand_summary})

    # ─── v0.3.2.1: run additional strategies (routes, future channels) ──
    # The symbol-grounding logic above is unchanged for backward
    # compatibility. The strategy registry runs additional, independent
    # checks (route grounding, semantic, LSP) and contributes findings
    # to new top-level keys without disturbing the existing arrays.
    routes_resolved: list[dict] = []
    routes_low_confidence: list[dict] = []
    routes_unknown: list[dict] = []
    pages_resolved: list[dict] = []
    pages_low_confidence: list[dict] = []
    pages_unknown: list[dict] = []
    env_resolved: list[dict] = []
    env_low_confidence: list[dict] = []
    env_unknown: list[dict] = []
    db_resolved: list[dict] = []
    db_low_confidence: list[dict] = []
    db_unknown: list[dict] = []
    gql_resolved: list[dict] = []
    gql_low_confidence: list[dict] = []
    gql_unknown: list[dict] = []
    flag_resolved: list[dict] = []
    flag_low_confidence: list[dict] = []
    flag_unknown: list[dict] = []
    css_resolved: list[dict] = []
    css_low_confidence: list[dict] = []
    css_unknown: list[dict] = []
    i18n_resolved: list[dict] = []
    i18n_low_confidence: list[dict] = []
    i18n_unknown: list[dict] = []
    npm_resolved: list[dict] = []
    npm_low_confidence: list[dict] = []
    npm_unknown: list[dict] = []
    asset_resolved: list[dict] = []
    asset_low_confidence: list[dict] = []
    asset_unknown: list[dict] = []
    import_resolved: list[dict] = []
    import_low_confidence: list[dict] = []
    import_unknown: list[dict] = []
    prose_resolved: list[dict] = []
    prose_low_confidence: list[dict] = []
    prose_unknown: list[dict] = []
    strategy_diagnostics: list[dict] = []
    try:
        from lint_strategies import (LintContext, IssueSeverity,
                                     enabled_strategies)
        ctx = LintContext(
            content=content,
            file_path=file_path,
            project_slug=project_slug,
            language=parser_lang,
            conn=conn,
            defined=defined,
            referenced=referenced,
            var_types=var_types,
            member_calls=member_calls,
        )
        for strat in enabled_strategies():
            if strat.name == "symbol_grounding":
                # Skip — the inline logic above already handled it.
                # (The strategy wrapper exists for future refactor symmetry.)
                continue
            if not strat.applies_to(ctx):
                continue
            try:
                strategy_issues = strat.check(ctx)
            except Exception as e:
                # A broken strategy must never block the rest of the lint.
                import sys
                sys.stderr.write(f"claude-dejavu: strategy '{strat.name}' raised: {type(e).__name__}: {e}\n")
                continue
            for iss in strategy_issues:
                # Per-strategy bucket: routes here, future strategies extend.
                bucket_resolved = bucket_low = bucket_unknown = None
                if iss.strategy == "route_grounding":
                    bucket_resolved = routes_resolved
                    bucket_low = routes_low_confidence
                    bucket_unknown = routes_unknown
                elif iss.strategy == "page_duplicate":
                    bucket_resolved = pages_resolved
                    bucket_low = pages_low_confidence
                    bucket_unknown = pages_unknown
                elif iss.strategy == "env_grounding":
                    bucket_resolved = env_resolved
                    bucket_low = env_low_confidence
                    bucket_unknown = env_unknown
                elif iss.strategy == "db_grounding":
                    bucket_resolved = db_resolved
                    bucket_low = db_low_confidence
                    bucket_unknown = db_unknown
                elif iss.strategy == "graphql_grounding":
                    bucket_resolved = gql_resolved
                    bucket_low = gql_low_confidence
                    bucket_unknown = gql_unknown
                elif iss.strategy == "flag_grounding":
                    bucket_resolved = flag_resolved
                    bucket_low = flag_low_confidence
                    bucket_unknown = flag_unknown
                elif iss.strategy == "css_grounding":
                    bucket_resolved = css_resolved
                    bucket_low = css_low_confidence
                    bucket_unknown = css_unknown
                elif iss.strategy == "i18n_grounding":
                    bucket_resolved = i18n_resolved
                    bucket_low = i18n_low_confidence
                    bucket_unknown = i18n_unknown
                elif iss.strategy == "npm_script_grounding":
                    bucket_resolved = npm_resolved
                    bucket_low = npm_low_confidence
                    bucket_unknown = npm_unknown
                elif iss.strategy == "asset_path_grounding":
                    bucket_resolved = asset_resolved
                    bucket_low = asset_low_confidence
                    bucket_unknown = asset_unknown
                elif iss.strategy == "import_grounding":
                    bucket_resolved = import_resolved
                    bucket_low = import_low_confidence
                    bucket_unknown = import_unknown
                elif iss.strategy == "prose_grounding":
                    bucket_resolved = prose_resolved
                    bucket_low = prose_low_confidence
                    bucket_unknown = prose_unknown
                else:
                    # Future strategies — diagnostic only for now
                    strategy_diagnostics.append({
                        "strategy": iss.strategy, "severity": iss.severity.value,
                        "name": iss.name, "candidate": iss.candidate,
                        "confidence": iss.confidence, "reason": iss.reason,
                        "fix": iss.fix, "metadata": iss.metadata,
                    })
                    continue
                rec = {
                    "name": iss.name, "candidate": iss.candidate,
                    "confidence": round(iss.confidence, 3),
                    "reason": iss.reason, "fix": iss.fix,
                    "metadata": iss.metadata, "strategy": iss.strategy,
                }
                if iss.severity == IssueSeverity.OK:
                    bucket_resolved.append(rec)
                elif iss.severity == IssueSeverity.LOW_CONFIDENCE:
                    bucket_low.append(rec)
                else:
                    bucket_unknown.append(rec)
    except Exception as e:
        import sys
        sys.stderr.write(f"claude-dejavu: strategies pipeline failed: {type(e).__name__}: {e}\n")

    # Final verdict folds in every domain's findings.
    has_unknown = (bool(unknown) or bool(routes_unknown)
                   or bool(pages_unknown) or bool(env_unknown)
                   or bool(db_unknown) or bool(gql_unknown)
                   or bool(flag_unknown) or bool(css_unknown)
                   or bool(i18n_unknown) or bool(npm_unknown)
                   or bool(asset_unknown) or bool(import_unknown)
                   or bool(prose_unknown))
    has_warn = (bool(low_confidence) or bool(routes_low_confidence)
                or bool(pages_low_confidence) or bool(env_low_confidence)
                or bool(db_low_confidence) or bool(gql_low_confidence)
                or bool(flag_low_confidence) or bool(css_low_confidence)
                or bool(i18n_low_confidence) or bool(npm_low_confidence)
                or bool(asset_low_confidence) or bool(import_low_confidence)
                or bool(prose_low_confidence))
    if has_unknown:
        verdict = "block"
    elif has_warn:
        verdict = "warn"
    else:
        verdict = "ok"

    return {
        "language": lang,
        "resolved": resolved,
        "low_confidence": low_confidence,
        "unknown": unknown,
        "routes_resolved": routes_resolved,
        "routes_low_confidence": routes_low_confidence,
        "routes_unknown": routes_unknown,
        "pages_resolved": pages_resolved,
        "pages_low_confidence": pages_low_confidence,
        "pages_unknown": pages_unknown,
        "env_resolved": env_resolved,
        "env_low_confidence": env_low_confidence,
        "env_unknown": env_unknown,
        "db_resolved": db_resolved,
        "db_low_confidence": db_low_confidence,
        "db_unknown": db_unknown,
        "gql_resolved": gql_resolved,
        "gql_low_confidence": gql_low_confidence,
        "gql_unknown": gql_unknown,
        "flag_resolved": flag_resolved,
        "flag_low_confidence": flag_low_confidence,
        "flag_unknown": flag_unknown,
        "css_resolved": css_resolved,
        "css_low_confidence": css_low_confidence,
        "css_unknown": css_unknown,
        "i18n_resolved": i18n_resolved,
        "i18n_low_confidence": i18n_low_confidence,
        "i18n_unknown": i18n_unknown,
        "npm_resolved": npm_resolved,
        "npm_low_confidence": npm_low_confidence,
        "npm_unknown": npm_unknown,
        "asset_resolved": asset_resolved,
        "asset_low_confidence": asset_low_confidence,
        "asset_unknown": asset_unknown,
        "import_resolved": import_resolved,
        "import_low_confidence": import_low_confidence,
        "import_unknown": import_unknown,
        "prose_resolved": prose_resolved,
        "prose_low_confidence": prose_low_confidence,
        "prose_unknown": prose_unknown,
        "strategy_diagnostics": strategy_diagnostics,
        "skipped_builtins": skipped_builtins,
        "summary": {
            "total_references": (len(resolved) + len(low_confidence) + len(unknown)
                                  + len(routes_resolved) + len(routes_low_confidence)
                                  + len(routes_unknown)
                                  + len(pages_resolved) + len(pages_low_confidence)
                                  + len(pages_unknown)
                                  + len(env_resolved) + len(env_low_confidence)
                                  + len(env_unknown)
                                  + len(db_resolved) + len(db_low_confidence)
                                  + len(db_unknown)
                                  + len(gql_resolved) + len(gql_low_confidence)
                                  + len(gql_unknown)
                                  + len(flag_resolved) + len(flag_low_confidence)
                                  + len(flag_unknown)),
            "resolved": len(resolved),
            "low_confidence": len(low_confidence),
            "unknown": len(unknown),
            "routes_resolved": len(routes_resolved),
            "routes_low_confidence": len(routes_low_confidence),
            "routes_unknown": len(routes_unknown),
            "pages_resolved": len(pages_resolved),
            "pages_low_confidence": len(pages_low_confidence),
            "pages_unknown": len(pages_unknown),
            "env_resolved": len(env_resolved),
            "env_low_confidence": len(env_low_confidence),
            "env_unknown": len(env_unknown),
            "db_resolved": len(db_resolved),
            "db_low_confidence": len(db_low_confidence),
            "db_unknown": len(db_unknown),
            "gql_resolved": len(gql_resolved),
            "gql_low_confidence": len(gql_low_confidence),
            "gql_unknown": len(gql_unknown),
            "flag_resolved": len(flag_resolved),
            "flag_low_confidence": len(flag_low_confidence),
            "flag_unknown": len(flag_unknown),
            "verdict": verdict,
            "type_aware_resolved": sum(1 for x in resolved if x.get("via_receiver_type")),
            "type_aware_unknown": sum(1 for x in unknown if x.get("via_receiver_type")),
        },
    }
