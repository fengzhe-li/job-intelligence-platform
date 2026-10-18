"""JD-independent REPOSITORY CAPABILITY MAP, built from the repository's own files at one pinned commit.

Why (2026-10-14 audit): project evidence was only the hand-curated Evidence Bank items (four per project,
mostly citing README.md) and README text; no source file, route, component, endpoint, test, CI workflow,
container or script was ever read, so implemented capabilities that no curated item named (e.g. a React
multi-view frontend) were invisible to CV selection. This module reads the repository itself.

A map is a list of capability SLICES (a repository never has a single identity). Each slice names the
files that implement it, the facts extracted MECHANICALLY from them (page / component / route names,
endpoints, tool invocations, workflow and service names, counts) and an evidence grade:

* ``tested``               -- implemented in source AND exercised by the repository's own tests;
* ``directly_implemented`` -- implemented in source (not tests, not docs);
* ``dependency_declared``  -- only named in a manifest;
* ``documented_only``      -- only described in README/docs (recorded for the audit; never a claim).

README/docs are never implementation evidence. Statements (``statements_for``) are composed only from a
slice's extracted facts, so a CV bullet built from one can name nothing the code does not contain. Nothing
in the repository is executed; files are only read.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import tempfile

CACHE_NAME = "github_capability_maps.json"
MAP_VERSION = 2  # 2: records tracked_paths, so evidence citing a file can be checked against the commit
_SKIP_PARTS = {".git", "node_modules", "dist", "build", ".venv", "venv", "__pycache__", "site-packages",
               ".next", "coverage", "vendor"}
_MAX_FILE_BYTES = 400_000
_DOC_SUFFIXES = {".md", ".rst", ".txt"}
GRADES = ("tested", "directly_implemented", "dependency_declared", "documented_only")


def cache_path(root) -> Path:
    return Path(root) / "profile" / CACHE_NAME


# --- reading the tree ----------------------------------------------------------------------------------

class _Tree:
    def __init__(self, checkout: Path):
        self.root = Path(checkout)
        files = []
        for path in sorted(p for p in self.root.rglob("*") if p.is_file()):
            rel = path.relative_to(self.root)
            if any(part in _SKIP_PARTS for part in rel.parts[:-1]) or path.stat().st_size > _MAX_FILE_BYTES:
                continue
            files.append(rel.as_posix())
        self.files = files
        self._text: dict[str, str] = {}

    def text(self, rel: str) -> str:
        if rel not in self._text:
            try:
                self._text[rel] = (self.root / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                self._text[rel] = ""
        return self._text[rel]

    def with_suffix(self, *suffixes) -> list[str]:
        return [f for f in self.files if f.endswith(suffixes)]

    def under(self, prefix: str) -> list[str]:
        prefix = prefix.rstrip("/") + "/" if prefix else ""
        return [f for f in self.files if f.startswith(prefix)]


def is_test_path(rel: str) -> bool:
    parts = rel.casefold().split("/")
    name = parts[-1]
    return (any(p in {"test", "tests", "__tests__", "e2e", "spec"} for p in parts[:-1])
            or name.startswith("test_") or re.search(r"(_test\.py|\.test\.[jt]sx?|\.spec\.[jt]sx?)$", name) is not None)


def _is_doc(rel: str) -> bool:
    return Path(rel).suffix.casefold() in _DOC_SUFFIXES or rel.casefold().startswith(("docs/", "doc/"))


def _humanize(name: str) -> str:
    """'PitWallOutlook' -> 'Pit Wall Outlook'; 'task_detail' -> 'Task Detail'."""
    words = re.sub(r"([a-z])([A-Z])", r"\1 \2", re.sub(r"[_\-]+", " ", name)).split()
    return " ".join(w if w.isupper() else w[:1].upper() + w[1:] for w in words)


# --- detectors -----------------------------------------------------------------------------------------

_JS_FRAMEWORKS = {"react": "React", "vue": "Vue", "svelte": "Svelte", "@angular/core": "Angular", "next": "Next.js"}
_CHART_LIBS = {"recharts": "Recharts", "chart.js": "Chart.js", "react-chartjs-2": "Chart.js", "d3": "D3",
               "plotly.js": "Plotly", "react-plotly.js": "Plotly", "@nivo/core": "Nivo", "echarts": "ECharts",
               "victory": "Victory", "@visx/visx": "visx"}
_JS_TEST = {"vitest": "Vitest", "jest": "Jest", "@playwright/test": "Playwright", "cypress": "Cypress",
            "@testing-library/react": "React Testing Library"}


def _frontend_slices(tree: _Tree) -> list[dict]:
    slices = []
    for manifest in [f for f in tree.files if f.endswith("package.json") and not is_test_path(f)]:
        try:
            pkg = json.loads(tree.text(manifest) or "{}")
        except ValueError:
            continue
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        frameworks = [label for key, label in _JS_FRAMEWORKS.items() if key in deps]
        if not frameworks:
            continue
        base = manifest.rsplit("/", 1)[0] if "/" in manifest else ""
        src = [f for f in tree.under(base) if f.endswith((".tsx", ".jsx", ".ts", ".js", ".vue", ".svelte"))
               and not f.endswith((".config.ts", ".config.js", ".d.ts")) and "/test/" not in f]
        code = [f for f in src if not is_test_path(f)]
        tests = [f for f in src if is_test_path(f)]
        if not code:
            continue
        typescript = any(f.endswith((".ts", ".tsx")) for f in code)
        pages = sorted({Path(f).stem for f in code if Path(f).stem[:1].isupper() and f.endswith((".tsx", ".jsx", ".vue", ".svelte"))
                        and (re.search(r"/(pages|views|routes|screens)/", f) or re.search(r"(View|Page|Screen)$", Path(f).stem))})
        components = sorted({Path(f).stem for f in code if "/components/" in f and Path(f).stem[:1].isupper()})
        routes, api_paths, hooks, states, loading_error, labels = set(), set(), set(), 0, False, set()
        for f in code:
            text = tree.text(f)
            labels |= set(re.findall(r"\b(?:label|title|name):\s*[\"']([^\"']{3,40})[\"']", text))
            routes |= set(re.findall(r"<Route\s+[^>]*path=[\"']([^\"']+)[\"']", text))
            routes |= set(re.findall(r"\bpath:\s*[\"'](/[^\"']*)[\"']", text))
            api_paths |= set(re.findall(r"[\"'`](/api/[A-Za-z0-9_/\-{}$.:]*)", text))
            # template-literal calls: fetch(`${BASE}/tasks/${id}`) -> /tasks/${id}
            for call in re.findall(r"\b(?:fetch|get|post|put|patch|delete|request)\w*\(\s*`([^`]+)`", text):
                path = re.sub(r"^\$\{[^}]+\}", "", call)
                if path.startswith("/") and len(path) > 1:
                    api_paths.add(path.split("?")[0])
            hooks |= set(re.findall(r"\b(use[A-Z][A-Za-z]+)\s*\(", text))
            states += len(re.findall(r"\buseState\s*[<(]", text))
            loading_error = loading_error or bool(re.search(r"\b(isLoading|loading|setLoading)\b", text)
                                                    and re.search(r"\berror\b", text))
        custom_hooks = sorted(h for h in hooks if h not in {"useState", "useEffect", "useMemo", "useCallback", "useRef",
                                                            "useContext", "useReducer", "useParams", "useNavigate",
                                                            "useLocation", "useSearchParams", "useLayoutEffect"})
        api_clients = sorted(f for f in code if re.search(r"/(api|services|client)[/.]", f)
                             and re.search(r"\b(fetch|axios)\b", tree.text(f)))
        charts = sorted({label for key, label in _CHART_LIBS.items() if key in deps})
        build = "Vite" if "vite" in deps else ("Next.js" if "next" in deps else ("Create React App" if "react-scripts" in deps else ""))
        test_tools = sorted({label for key, label in _JS_TEST.items() if key in deps})
        def page_label(page):
            # the UI's own label for this page ("PitWallOutlook" -> "Pit-Wall Outlook") when exactly one matches
            page = re.sub(r"(View|Page|Screen)$", "", page) or page
            words = [w.casefold() for w in _humanize(page).split()]
            hits = [l for l in labels if all(re.search(rf"\b{re.escape(w)}", l.casefold()) for w in words)]
            return hits[0] if len(hits) == 1 else _humanize(page)
        test_cases = sum(len(re.findall(r"\b(?:it|test)\(\s*[\"'`]", tree.text(t))) for t in tests)
        facts = {"app_dir": base or ".", "frameworks": frameworks, "typescript": typescript, "build": build,
                 "pages": [page_label(p) for p in pages], "routes": sorted(routes), "components": len(components),
                 "test_cases": test_cases,
                 "component_names": [_humanize(c) for c in components][:12], "custom_hooks": custom_hooks,
                 "use_state_count": states, "api_client_files": api_clients, "api_paths": sorted(api_paths)[:12],
                 "chart_libraries": charts, "loading_error_states": loading_error,
                 "test_files": len(tests), "test_tools": test_tools}
        caps = frameworks + (["TypeScript"] if typescript else ["JavaScript"]) + ([build] if build in {"Vite", "Next.js"} else []) \
            + charts + (["REST"] if api_clients else [])
        slices.append(_slice(f"frontend:{base or '.'}", "Frontend application", ["frontend", "web", "ui"],
                             facts, code[:40], "tested" if tests else "directly_implemented", caps, tests[:20]))
    return slices


_ROUTE = re.compile(r"@(?:app|router|api|bp|blueprint)\.(get|post|put|patch|delete)\(\s*[\"']([^\"']+)[\"']", re.I)
_FLASK_ROUTE = re.compile(r"@(?:app|bp|blueprint)\.route\(\s*[\"']([^\"']+)[\"']")


def _python_imports(text: str) -> set[str]:
    out = set()
    for m in re.finditer(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+(?:\s*,\s*[\w.]+)*))", text, re.M):
        for name in (m.group(1) or m.group(2) or "").split(","):
            if name.strip():
                out.add(name.strip().split(".")[0])
    return out


def _backend_slices(tree: _Tree) -> list[dict]:
    py = [f for f in tree.with_suffix(".py") if not _is_doc(f)]
    code = [f for f in py if not is_test_path(f)]
    tests = [f for f in py if is_test_path(f)]
    slices = []
    endpoints, frameworks, files, pyd = [], set(), [], 0
    for f in code:
        text = tree.text(f)
        found = [(m.group(1).upper(), m.group(2)) for m in _ROUTE.finditer(text)] + \
                [("ROUTE", m.group(1)) for m in _FLASK_ROUTE.finditer(text)]
        imports = _python_imports(text)
        if found:
            files.append(f)
            endpoints += found
            frameworks |= {label for key, label in (("fastapi", "FastAPI"), ("flask", "Flask"), ("django", "Django"))
                           if key in imports}
        if "pydantic" in imports:
            pyd += len(re.findall(r"class\s+\w+\(BaseModel\)", text))
    if endpoints:
        tested = [t for t in tests if re.search(r"\b(TestClient|client\.(get|post|put|delete))\b", tree.text(t))]
        slices.append(_slice("backend:http_api", "HTTP API service", ["backend", "api"],
                             {"frameworks": sorted(frameworks) or ["HTTP"], "endpoints": sorted(set(endpoints))[:25],
                              "endpoint_count": len(set(endpoints)), "pydantic_models": pyd, "files": files[:10],
                              "api_test_files": len(tested)},
                             files, "tested" if tested else "directly_implemented",
                             sorted(frameworks) + ["Python", "REST"] + (["Pydantic"] if pyd else []), tested[:20]))
    dash = [f for f in code if "streamlit" in _python_imports(tree.text(f))]
    if dash:
        widgets = set()
        for f in dash:
            widgets |= set(re.findall(r"\bst\.(dataframe|table|plotly_chart|line_chart|bar_chart|altair_chart|metric|"
                                      r"selectbox|multiselect|slider|text_input|button|tabs|columns|expander|sidebar|"
                                      r"file_uploader|form|chat_input|download_button)\b", tree.text(f)))
        slices.append(_slice("ui:streamlit", "Streamlit dashboard", ["ui", "dashboard", "internal_tooling"],
                             {"files": dash, "widgets": sorted(widgets)}, dash, "directly_implemented",
                             ["Streamlit", "Python"], []))
    return slices


_DATA_STORES = {"psycopg": "PostgreSQL", "psycopg2": "PostgreSQL", "asyncpg": "PostgreSQL", "sqlalchemy": "SQLAlchemy",
                "qdrant_client": "Qdrant", "redis": "Redis", "sqlite3": "SQLite", "pymongo": "MongoDB",
                "elasticsearch": "Elasticsearch", "duckdb": "DuckDB"}
_ML = {"numpy": "NumPy", "pandas": "pandas", "scipy": "SciPy", "sklearn": "scikit-learn", "torch": "PyTorch",
       "tensorflow": "TensorFlow", "keras": "Keras", "statsmodels": "statsmodels", "xgboost": "XGBoost",
       "librosa": "librosa", "cv2": "OpenCV", "matplotlib": "Matplotlib"}
_LLM = {"openai": "OpenAI API", "anthropic": "Anthropic API", "google": "Google GenAI", "langchain": "LangChain",
        "transformers": "Hugging Face Transformers", "llama_index": "LlamaIndex"}
_CLOUD = {"boto3": "AWS", "botocore": "AWS", "paho": "MQTT", "awsiot": "AWS IoT",
          "google.cloud": "GCP", "azure": "Azure"}


def _library_slices(tree: _Tree) -> list[dict]:
    code = [f for f in tree.with_suffix(".py") if not is_test_path(f) and not _is_doc(f)]
    tests = [f for f in tree.with_suffix(".py") if is_test_path(f)]
    by_lib: dict[str, set[str]] = {}
    for f in code:
        imports = _python_imports(tree.text(f))
        for table, kind in ((_DATA_STORES, "data"), (_ML, "ml"), (_LLM, "llm"), (_CLOUD, "cloud")):
            for key, label in table.items():
                dotted = "." in key and re.search(rf"^\s*(?:from|import)\s+{re.escape(key)}\b", tree.text(f), re.M)
                if (dotted or ("." not in key and key in imports)) and (key != "google" or "genai" in tree.text(f)):
                    by_lib.setdefault(f"{kind}|{label}", set()).add(f)
    for f in code:
        text = tree.text(f)
        for label in ("Qdrant", "PostgreSQL", "Redis", "Elasticsearch", "DynamoDB", "MongoDB"):
            stem = {"PostgreSQL": "Postgres"}.get(label, label)
            if re.search(rf"^\s*class\s+\w*{stem}\w*\s*[(:]", text, re.M):
                by_lib.setdefault(f"data|{label}", set()).add(f)
    tested_imports = set()
    for t in tests:
        tested_imports |= _python_imports(tree.text(t))
    groups: dict[str, dict[str, set[str]]] = {}
    for key, files in by_lib.items():
        kind, label = key.split("|", 1)
        groups.setdefault(kind, {})[label] = files
    titles = {"data": ("Data stores", ["data", "backend"]), "ml": ("Scientific / ML computing", ["ml", "science"]),
              "llm": ("LLM / generative-AI integration", ["ai", "llm"]), "cloud": ("Cloud / IoT services", ["cloud", "iot"])}
    slices = []
    for kind, libs in groups.items():
        files = sorted({f for fs in libs.values() for f in fs})
        migrations = [f for f in tree.files if re.search(r"(^|/)(alembic|migrations)/versions/.+\.py$", f)] if kind == "data" else []
        aws_services = sorted({m for f in files for m in re.findall(r"boto3\.(?:client|resource)\(\s*[\"']([a-z0-9\-]+)", tree.text(f))}) \
            if kind == "cloud" else []
        facts = {"libraries": sorted(libs), "files": files[:12]}
        if migrations:
            facts["migrations"] = len(migrations)
        if kind == "data" and len(libs) < 2 and not migrations:
            continue  # a single plain data library is not a capability worth a slice
        if aws_services:
            facts["aws_services"] = aws_services
        title, kinds = titles[kind]
        slices.append(_slice(f"{kind}:libraries", title, kinds, facts, files,
                             "tested" if tests and any(Path(f).stem in tested_imports for f in files) else "directly_implemented",
                             sorted(libs) + (["Alembic"] if migrations else []), []))
    return slices


_SYS_TOOLS = {"criu": r"\bcriu\b", "iptables": r"\biptables\b", "nftables": r"\bnft\s", "ip-addr": r"\bip\s+(?:addr|address|a)\b",
              "ip-route": r"\bip\s+route\b", "arping/GARP": r"\barping\b|\bgarp\b|ARP\(", "ssh": r"\bssh\s|\bparamiko\b",
              "scp/rsync": r"\b(?:scp|rsync)\s", "tc": r"\btc\s+(?:qdisc|filter)\b", "ss/netstat": r"\b(?:ss|netstat)\s+-",
              "systemctl": r"\bsystemctl\b", "tcpdump": r"\btcpdump\b", "docker checkpoint": r"\bdocker\s+checkpoint\b",
              "scapy": r"\bscapy\b"}


# Concrete operations, each from the command that performs it (never from a file name or README).
_SYS_OPERATIONS = (
    ("CRIU checkpoint (dump)", r"\bcriu\s+(?:-\S+\s+)*dump\b"),
    ("CRIU restore", r"\bcriu\s+(?:-\S+\s+)*restore\b"),
    ("Docker container checkpoint", r"\bdocker\s+checkpoint\s+create\b"),
    ("iptables traffic lock/unlock", r"\biptables\s+-[IA]\s+(?:INPUT|OUTPUT|FORWARD)\b"),
    ("virtual IP claim/release", r"\bip\s+(?:addr|address)\s+(?:add|del)\b"),
    ("gratuitous ARP announcement", r"\barping\b|\bsend_garp\b|ARP\(\s*op\s*=\s*2|\bgarp\b"),
    ("remote orchestration over SSH", r"^\s*[^#\n]*\bssh\s+"),
    ("checkpoint/runtime transfer (scp/rsync)", r"^\s*[^#\n]*\b(?:scp|rsync)\s+"),
    ("persistent TCP client/server", r"socket\.socket\(|socket\.create_connection\("),
    ("process tracing (strace)", r"\bstrace\b"),
)


def _systems_slice(tree: _Tree) -> list[dict]:
    scripts = [f for f in tree.files if (f.endswith((".sh", ".py")) or "/bin/" in f) and not is_test_path(f) and not _is_doc(f)]
    used: dict[str, set[str]] = {}
    flags: set[str] = set()
    timing = set()
    operations: dict[str, set[str]] = {}
    for f in scripts:
        text = tree.text(f)
        for tool, pattern in _SYS_TOOLS.items():
            if re.search(pattern, text):
                used.setdefault(tool, set()).add(f)
        if re.search(r"\bcriu\b", text):
            flags |= set(re.findall(r"(--(?:tcp-established|tcp-close|shell-job|leave-running|leave-stopped|ext-unix-sk|"
                                    r"file-locks|images-dir|track-mem|pre-dump|lazy-pages|page-server))\b", text))
        if re.search(r"date\s+\+%s%N|time\.perf_counter|time\.monotonic|\$\(\(\s*\$\w+\s*-\s*\$\w+", text):
            timing.add(f)
        for op, pattern in _SYS_OPERATIONS:
            if re.search(pattern, text, re.M):
                operations.setdefault(op, set()).add(f)
    if len(used) < 2:
        return []
    files = sorted({f for fs in used.values() for f in fs})
    caps = ["Linux"] + (["CRIU"] if "criu" in used else []) + (["Bash"] if any(f.endswith(".sh") for f in files) else []) \
        + (["TCP/IP"] if used.keys() & {"iptables", "ip-addr", "ip-route", "arping/GARP", "tc", "ss/netstat", "scapy"} else []) \
        + (["SSH"] if "ssh" in used else [])
    return [_slice("systems:linux_ops", "Linux systems / network operations", ["systems", "networking", "operations"],
                   {"tools": sorted(used), "criu_flags": sorted(flags), "scripts": len(files), "files": files[:15],
                    "operations": [op for op, _ in _SYS_OPERATIONS if op in operations],
                    "timing_instrumented_files": sorted(timing)[:6]},
                   files, "directly_implemented", caps, [])]


_VENDOR_C = re.compile(r"(^|/)(Drivers|Middlewares|CMSIS|Libraries|STM32[^/]*_StdPeriph_Driver|HAL_Driver|lib|third_party)/", re.I)
_BUS = (("I2C", r"\bI2C\w*\(|\bIIC\w*\(|i2c|iic"), ("SPI", r"\bSPI\w*_\w+\("), ("UART", r"\bU?S?ART\d?_\w+\(|usart|uart"),
        ("timers", r"\bTIM\d?_\w+\(|\btimer"), ("ADC", r"\bADC\d?_\w+\("), ("GPIO", r"\bGPIO_\w+\(|GPIO[A-G]->"),
        ("interrupts", r"\bNVIC_\w+\(|\bEXTI_\w+\(|_IRQHandler\b"), ("flash", r"\bFLASH_\w+\(|stmflash"))


def _embedded_slice(tree: _Tree) -> list[dict]:
    user_c = [f for f in tree.with_suffix(".c") if not _VENDOR_C.search(f) and not is_test_path(f)]
    vendor = any(_VENDOR_C.search(f) for f in tree.with_suffix(".c", ".h"))
    mcu = any(re.search(r"\b(HAL_\w+|stm32\w*\.h|GPIO_Init|RCC_\w+ClockCmd|__IO\b)", tree.text(f)) for f in user_c)
    if not (user_c and (mcu or vendor)):
        return []
    buses = sorted({bus for f in user_c for bus, pattern in _BUS if re.search(pattern, tree.text(f) + " " + f, re.I)})
    modules = sorted({Path(f).stem for f in user_c} - {"main", "delay", "sys", "stm32f10x_it", "system_stm32f10x"})
    api = "HAL" if any("HAL_" in tree.text(f) for f in user_c) else "Standard Peripheral Library / registers"
    caps = ["C", "Embedded"] + [b for b in buses if b in {"I2C", "SPI", "UART"}]
    return [_slice("embedded:firmware", "Embedded firmware", ["embedded"],
                   {"c_files": len(user_c), "modules": modules, "buses": buses, "api": api, "files": user_c[:15]},
                   user_c, "directly_implemented", caps, [])]


def _tests_slice(tree: _Tree) -> list[dict]:
    py_tests = [f for f in tree.with_suffix(".py") if is_test_path(f)]
    js_tests = [f for f in tree.files if re.search(r"\.(test|spec)\.[jt]sx?$", f)]
    if not (py_tests or js_tests):
        return []
    py_count = sum(len(re.findall(r"^\s*(?:async\s+)?def\s+test_", tree.text(f), re.M)) for f in py_tests)
    js_count = sum(len(re.findall(r"\b(?:it|test)\(\s*[\"'`]", tree.text(f))) for f in js_tests)
    caps = (["pytest"] if py_tests else []) + (["Vitest"] if js_tests and any("vitest" in tree.text(p) for p in tree.files
                                                                             if p.endswith("package.json")) else [])
    return [_slice("quality:tests", "Automated tests", ["testing", "quality"],
                   {"python_test_files": len(py_tests), "python_test_functions": py_count,
                    "js_test_files": len(js_tests), "js_test_cases": js_count},
                   py_tests[:20] + js_tests[:20], "tested", caps, py_tests + js_tests)]


_PY_CLASS = re.compile(r"^\s*class (\w+)\s*(?:\(([^)]*)\))?\s*:", re.M)


def _design_slice(tree: _Tree) -> list[dict]:
    """Object-oriented design, from the code: an abstraction the repository itself defines (an `ABC` or
    `Protocol` class) AND at least one concrete class that explicitly implements it. Abstractions without an
    implementation, ORM / model / exception bases and third-party bases are not counted."""
    code = [f for f in tree.with_suffix(".py") if not is_test_path(f) and not _is_doc(f)]
    classes = []
    for f in code:
        for name, bases in _PY_CLASS.findall(tree.text(f)):
            classes.append((f, name, [b.strip().split("[")[0].split(".")[-1] for b in (bases or "").split(",") if b.strip()]))
    abstractions = {name: f for f, name, bases in classes if any(b in {"ABC", "Protocol"} for b in bases)}
    implementations: dict[str, list[str]] = {}
    files = set()
    for f, name, bases in classes:
        for base in bases:
            if base in abstractions and name not in abstractions:
                implementations.setdefault(base, []).append(name)
                files |= {f, abstractions[base]}
    if not implementations:
        return []
    names = sorted({impl for impls in implementations.values() for impl in impls})
    tests = [t for t in tree.with_suffix(".py") if is_test_path(t) and any(re.search(rf"\b{n}\b", tree.text(t)) for n in names)]
    return [_slice("design:oop", "Object-oriented design", ["design"],
                   {"abstractions": sorted(implementations), "implementations": names},
                   sorted(files), "tested" if tests else "directly_implemented", ["Object-Oriented Design"], tests)]


def _ci_slice(tree: _Tree) -> list[dict]:
    workflows = [f for f in tree.files if re.match(r"\.github/workflows/[^/]+\.ya?ml$", f)]
    if not workflows:
        return []
    names, jobs, steps = [], [], set()
    for f in workflows:
        text = tree.text(f)
        m = re.search(r"^name:\s*(.+)$", text, re.M)
        names.append((m.group(1).strip().strip("'\"") if m else Path(f).stem))
        jobs += re.findall(r"^  ([A-Za-z0-9_\-]+):\s*$", text, re.M)
        for label, pattern in (("pytest", r"\bpytest\b"), ("npm test", r"\bnpm (?:run )?test\b|\bvitest\b"),
                               ("lint", r"\b(ruff|flake8|eslint|black|mypy)\b"), ("docker build", r"docker (?:build|compose)"),
                               ("type check", r"\btsc\b|\bmypy\b"), ("frontend build", r"npm run build")):
            if re.search(pattern, text):
                steps.add(label)
    return [_slice("delivery:ci", "CI pipeline", ["ci", "devops"],
                   {"workflows": names, "jobs": sorted(set(jobs))[:12], "steps": sorted(steps)},
                   workflows, "directly_implemented", ["CI/CD", "GitHub Actions"], [])]


def _container_slice(tree: _Tree) -> list[dict]:
    dockerfiles = [f for f in tree.files if re.search(r"(^|/)Dockerfile[^/]*$", f)]
    composes = [f for f in tree.files if re.search(r"(^|/)(docker-)?compose[^/]*\.ya?ml$", f)]
    if not (dockerfiles or composes):
        return []
    services = []
    for f in composes:
        block = re.search(r"^services:\s*\n((?:[ \t]+.*\n?|\n)+)", tree.text(f), re.M)
        if block:
            services += re.findall(r"^  ([A-Za-z0-9_\-]+):\s*$", block.group(1), re.M)
    return [_slice("delivery:containers", "Containerisation", ["containers", "devops"],
                   {"dockerfiles": dockerfiles[:10], "compose_files": composes[:5], "compose_services": sorted(set(services))},
                   dockerfiles + composes, "directly_implemented", ["Docker"] + (["Docker Compose"] if composes else []), [])]


_ARTIFACT_DIR = re.compile(r"(^|/)(results?|outputs?|benchmarks?|experiments?|evaluation|reports?|runs|validation)(/|$)", re.I)
_ARTIFACT_SUFFIX = (".json", ".csv", ".jsonl", ".tsv", ".parquet", ".png", ".svg", ".log", ".txt", ".md")


def validation_artifacts(tree: _Tree) -> dict:
    """Generated validation / experiment / benchmark outputs committed to the repository (audit and provenance
    only: their numbers are never turned into CV claims automatically)."""
    files = [f for f in tree.files if _ARTIFACT_DIR.search(f) and f.endswith(_ARTIFACT_SUFFIX)
             and not f.endswith(("README.md",)) and not is_test_path(f)]
    reports = [f for f in tree.files if re.search(r"(REPORT|RESULTS|BENCHMARK|EVALUATION|VALIDATION)[^/]*\.md$", f, re.I)]
    dirs = sorted({re.split(r"/(?:results?|outputs?|runs)/", f)[0] for f in files if "/" in f})[:15]
    return {"result_files": len(files), "report_documents": sorted(reports)[:15], "artifact_dirs": dirs,
            "examples": sorted(files)[:10]}


def _documented_only(tree: _Tree, found_caps: set[str]) -> list[str]:
    """Technologies the README/docs name that no implementation slice has (audit only; never a claim)."""
    from jobintel.analysis.role_tracks import SKILL_PATTERNS, _skill_pattern
    docs = " ".join(tree.text(f) for f in tree.files if _is_doc(f) and f.casefold().endswith(".md"))[:400_000]
    known = {c.casefold() for c in found_caps}
    return sorted(s for s in SKILL_PATTERNS if s.casefold() not in known and _skill_pattern(s).search(docs))[:40]


def _slice(slice_id, title, kinds, facts, files, grade, capability_names, test_files) -> dict:
    return {"id": slice_id, "title": title, "kinds": kinds, "facts": facts, "files": sorted(set(files))[:40],
            "grade": grade, "capability_names": sorted(dict.fromkeys(c for c in capability_names if c)),
            "test_files": sorted(set(test_files))[:20]}


_LANG_SUFFIX = {".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript",
                ".c": "C", ".h": "C", ".cpp": "C++", ".hpp": "C++", ".cc": "C++", ".sh": "Shell", ".go": "Go", ".rs": "Rust",
                ".java": "Java", ".kt": "Kotlin", ".cs": "C#", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift", ".scala": "Scala"}


def repository_languages(tree: "_Tree") -> dict[str, int]:
    """GitHub-style language -> source file count at this commit (vendored, test-fixture and doc files excluded)."""
    counts: dict[str, int] = {}
    for f in tree.files:
        lang = _LANG_SUFFIX.get(Path(f).suffix.casefold())
        if lang and not _is_doc(f) and not re.search(r"(^|/)(Libraries|Drivers|CMSIS|third_party|vendor)/", f):
            counts[lang] = counts.get(lang, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def scan_checkout(checkout) -> dict:
    tree = _Tree(Path(checkout))
    slices = (_frontend_slices(tree) + _backend_slices(tree) + _library_slices(tree) + _systems_slice(tree)
              + _embedded_slice(tree) + _design_slice(tree) + _tests_slice(tree) + _ci_slice(tree) + _container_slice(tree))
    found = {c for s in slices for c in s["capability_names"]}
    return {"map_version": MAP_VERSION, "files_scanned": len(tree.files), "languages": repository_languages(tree),
            "validation_artifacts": validation_artifacts(tree),
            "source_files": len([f for f in tree.files if not _is_doc(f)]),
            "slices": slices, "documented_only": _documented_only(tree, found)}


# --- statements: CV-ready sentences composed ONLY from a slice's extracted facts ----------------------------

def _join(items, limit=5) -> str:
    items = [i for i in items if i][:limit]
    return ", ".join(items[:-1]) + (" and " if len(items) > 1 else "") + items[-1] if items else ""


def statements_for(slice_: dict) -> list[dict]:
    """[{key, text, capability_names}] for one slice. Every noun phrase comes from `facts`; a statement is
    produced only when its facts exist. Never a metric, scale, user count or outcome that the code does not show."""
    f, sid, out = slice_["facts"], slice_["id"], []

    def add(key, text, caps):
        out.append({"key": f"{sid}:{key}", "text": text, "capability_names": caps})

    caps = slice_["capability_names"]
    if sid.startswith("frontend:"):
        stack = _join(f["frameworks"] + (["TypeScript"] if f["typescript"] else []), 3)
        build = f" ({f['build']})" if f["build"] in {"Vite", "Next.js"} else ""
        if f["pages"]:
            add("views", f"Built a {stack}{build} frontend with {len(f['pages'])} views ({_join(f['pages'], 5)})"
                         f"{' and ' + str(f['components']) + ' reusable components' if f['components'] >= 3 else ''}.", caps)
        elif f["components"] >= 3:
            add("views", f"Built a {stack}{build} frontend with {f['components']} reusable components.", caps)
        if f["api_client_files"]:
            extras = [x for x in (("charts (" + _join(f["chart_libraries"], 2) + ")") if f["chart_libraries"] else "",
                                  "loading and error states" if f["loading_error_states"] else "",
                                  ("a custom " + f["custom_hooks"][0] + " hook") if f["custom_hooks"] else "") if x]
            add("integration", f"Integrated the UI with the backend through a {'typed ' if f['typescript'] else ''}API client"
                               f"{' (' + _join(f['api_paths'], 3) + ')' if f['api_paths'] else ''}"
                               f"{', rendering results with ' + _join(extras, 3) if extras else ''}.", caps + ["REST"])
        if f["routes"] and not f["pages"]:
            add("routes", f"Implemented client-side routing across {len(f['routes'])} routes ({_join(f['routes'], 4)}).", caps)
        if f["test_files"]:
            count = (f"{f['test_cases']} component/page test cases" if f.get("test_cases")
                     else f"{f['test_files']} component/page test files")
            add("tests", f"Tested the frontend with {count}"
                         f"{' (' + _join(f['test_tools'], 2) + ')' if f['test_tools'] else ''}.", caps + f["test_tools"])
    elif sid == "backend:http_api":
        fw = _join(f["frameworks"], 2)
        sample = _join([f"{m} {p}" for m, p in f["endpoints"][:4]], 4)
        tested = (f", tested through {f['api_test_files']} HTTP-level test file{'s' if f['api_test_files'] != 1 else ''}"
                  if f["api_test_files"] else "")
        add("endpoints", f"Implemented a {fw} HTTP API with {f['endpoint_count']} endpoints (e.g. {sample})"
                         f"{' and ' + str(f['pydantic_models']) + ' Pydantic request/response models' if f['pydantic_models'] else ''}"
                         f"{tested}.", caps + (["pytest"] if tested else []))
    elif sid == "ui:streamlit":
        add("dashboard", f"Built a Streamlit dashboard"
                         f"{' using ' + _join(f['widgets'], 5) if f['widgets'] else ''} for interactive inspection.", caps)
    elif sid == "systems:linux_ops":
        ops, crit = f.get("operations") or [], (f" (CRIU {_join(f['criu_flags'], 3)})" if f["criu_flags"] else "")
        if ops:
            add("orchestration", f"Automated {_join(ops[:5], 5)}{crit} across {f['scripts']} Bash/Python scripts.", caps)
            if len(ops) > 5:
                add("operations", f"Automated {_join(ops[5:], 4)} as part of the same Linux workflow.", caps)
        else:
            tools = [t for t in f["tools"] if t not in {"ss/netstat"}]
            add("orchestration", f"Scripted Linux systems/network operations across {f['scripts']} scripts using {_join(tools, 6)}{crit}.", caps)
        if f["timing_instrumented_files"]:
            add("timing", f"Implemented timing measurements in {len(f['timing_instrumented_files'])} operational scripts.", caps)
    elif sid == "embedded:firmware":
        mods = [m.upper() if re.fullmatch(r"[a-z]+\d+[a-z]*\d*", m) else _humanize(m) for m in f["modules"]]
        add("firmware", f"Implemented embedded C firmware ({f['c_files']} source modules, {f['api']})"
                        f"{' with drivers/modules for ' + _join(mods, 6) if mods else ''}.", caps)
        if f["buses"]:
            add("peripherals", f"Implemented direct C control of the microcontroller's {_join(f['buses'], 6)}.", caps)
    elif sid == "design:oop":
        abstractions, impls = f["abstractions"], f["implementations"]
        add("interfaces", f"Applied object-oriented design: {len(abstractions)} abstract interface"
                          f"{'s' if len(abstractions) != 1 else ''} ({_join(abstractions, 3)}) with {len(impls)} "
                          f"interchangeable implementation{'s' if len(impls) != 1 else ''} ({_join(impls, 3)}).", caps)
    elif sid == "quality:tests":
        parts = [x for x in (f"{f['python_test_functions']} pytest tests" if f["python_test_functions"] else "",
                             f"{f['js_test_cases']} frontend test cases" if f["js_test_cases"] else "") if x]
        if parts:
            add("suite", f"Tested the codebase with an automated suite of {_join(parts, 2)}.", caps)
    elif sid == "delivery:ci":
        add("pipeline", f"Automated CI with GitHub Actions ({_join(f['workflows'], 2)})"
                        f"{' running ' + _join(f['steps'], 4) if f['steps'] else ''}.", caps)
    elif sid == "delivery:containers":
        svc = f" ({_join(f['compose_services'], 4)})" if f["compose_services"] else ""
        n = len(f["dockerfiles"])
        add("containers", f"Packaged the system as Docker images ({n} Dockerfile{'s' if n != 1 else ''})"
                          f"{' with Docker Compose services' + svc if f['compose_files'] else ''}.", caps)
    elif sid.split(":")[0] in {"data", "ml", "llm", "cloud"}:
        kind = sid.split(":")[0]
        if kind == "cloud" and f.get("aws_services"):
            names = {"iot-data": "IoT Data", "ses": "SES", "sesv2": "SES", "s3": "S3", "sqs": "SQS", "sns": "SNS",
                     "dynamodb": "DynamoDB", "lambda": "Lambda", "iot": "IoT Core"}
            services = list(dict.fromkeys(names.get(x, x.title()) for x in f["aws_services"]))
            add("libraries", f"Integrated AWS services ({_join(services, 5)}) through boto3.", caps)
        else:
            libs = _join(f["libraries"], 4)
            if kind == "llm":
                modules = sorted({Path(x).stem.replace("_", " ") for x in f["files"]})[:3]
                add("libraries", f"Integrated {libs} as a model provider ({_join(modules, 3)} module"
                                 f"{'s' if len(modules) != 1 else ''}).", caps)
            else:
                verb = {"data": "Implemented data persistence with", "ml": "Implemented the numerical/ML computation with",
                        "cloud": "Integrated cloud services through"}[kind]
                extra = f" ({f['migrations']} schema migration{'s' if f['migrations'] != 1 else ''})" if f.get("migrations") else ""
                add("libraries", f"{verb} {libs}{extra}.", caps)
    return out


# --- collecting and caching --------------------------------------------------------------------------------

def _git(*args, cwd=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}).stdout.strip()


def build_map(full_name: str, *, checkout=None, clock=None) -> dict:
    """Clone (or read an origin-verified checkout of) the exact repository and map it at its HEAD commit."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", full_name or ""):
        raise ValueError(f"Not an exact owner/name repository: {full_name!r}")
    with tempfile.TemporaryDirectory(prefix="jobintel-capmap-") as tmp:
        if checkout is None:
            checkout = Path(tmp) / "repo"
            _git("clone", "--quiet", "--depth", "1", "--no-tags", f"https://github.com/{full_name}.git", str(checkout))
        checkout = Path(checkout)
        origin = re.sub(r"\.git$", "", _git("remote", "get-url", "origin", cwd=checkout).rstrip("/"))
        if not re.search(rf"github\.com[:/]{re.escape(full_name)}$", origin, re.I):
            raise ValueError(f"Checkout origin is not github.com/{full_name}")
        commit = _git("rev-parse", "HEAD", cwd=checkout)
        mapped = scan_checkout(checkout)
        tracked = sorted(p for p in _git("ls-files", cwd=checkout).splitlines() if p)
    return {"repository": full_name, "commit": commit, "source_url": f"https://github.com/{full_name}",
            "built_at": (clock() if clock else datetime.now(timezone.utc)).isoformat(), "tracked_paths": tracked,
            **mapped}


def path_status(record: dict | None, path: str | None) -> str:
    """Whether a cited file exists in the repository at the map's pinned commit:
    'present', 'missing', or 'unverifiable' (no map, or a map built before tracked paths were recorded)."""
    if not record or not isinstance(record.get("tracked_paths"), list):
        return "unverifiable"
    rel = (path or "").strip().lstrip("./")
    if not rel:
        return "unverifiable"
    return "present" if rel in set(record["tracked_paths"]) else "missing"


def save_map(root, record: dict) -> None:
    path = cache_path(root)
    cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cache[record["repository"]] = record
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def live_head(full_name: str) -> str | None:
    """The repository's current HEAD commit on GitHub (read-only `git ls-remote`), or None."""
    try:
        line = _git("ls-remote", f"https://github.com/{full_name}.git", "HEAD")
    except Exception:  # noqa: BLE001
        return None
    return line.split()[0] if line else None


def stale_maps(root, heads: dict) -> list[dict]:
    """Repositories (name -> live HEAD commit) whose capability map is missing or not at that commit."""
    maps, out = load_maps(root), []
    for name, sha in heads.items():
        record = maps.get(name)
        if record is None or (sha and record.get("commit") != sha):
            out.append({"repository": name, "map_commit": (record or {}).get("commit"), "live_head": sha})
    return out


def load_maps(root) -> dict:
    try:
        return json.loads(cache_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def map_languages(root, full_name: str | None) -> set[str] | None:
    """Languages (casefolded) present in the project's own source at its mapped commit, or None."""
    record = load_maps(root).get(full_name or "")
    return {name.casefold() for name in record.get("languages", {})} if record and record.get("languages") else None


def corroboration_text(root, full_name: str | None) -> str | None:
    """Capability names and extracted facts of the project's implemented slices (never documented-only)."""
    record = load_maps(root).get(full_name or "")
    if not record:
        return None
    parts = []
    for s in record.get("slices", []):
        if s["grade"] in {"tested", "directly_implemented"}:
            parts.append(" ".join(s["capability_names"]))
            parts += [st["text"] for st in statements_for(s)]
    return "\n".join(parts) or None
