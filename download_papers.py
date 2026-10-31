"""
Past paper downloader + compiler for bestexamhelp.com   (v5)
--------------------------------------------------------------
Just run:   python download_papers.py          (it asks you what you want)

Handy extras:
    python download_papers.py --help           all command-line options
    python download_papers.py --subject physics-9702 --years 2019-2024 --types qp,ms --yes
    python download_papers.py --saved --dry-run    show what WOULD be downloaded, download nothing
    python download_papers.py --self-test          run the built-in tests (no internet needed)

What it does:
  1. Checks the Python libraries it needs (and asks before installing anything).
  2. Reads the site's listing page for every subject + year to see EXACTLY what exists.
  3. Shows a plan (file counts, rough time) and asks before it starts.
  4. Downloads in parallel into  papers/<Subject code>/<year>/  . Files are written safely
     (never half-finished), and anything incomplete is re-downloaded next run.
  5. Prints a summary, saves failed_links.txt if anything failed, and can merge each subject
     into bookmarked PDFs.

Please be kind to the site: it's a free educational service. Keep the delays/workers modest
and don't re-run huge downloads needlessly (files you already have are skipped).
Check the site's terms of use for what's allowed.
Needs Python 3.9 or newer.
"""

import argparse
import datetime
import importlib
import itertools
import os
import platform
import random
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

if sys.version_info < (3, 9):
    sys.exit("This script needs Python 3.9 or newer.")

# =====================================================================
# STEP 0: make sure required libraries are installed (asks first)
# =====================================================================
# Format:  "import name": "pip package name"
REQUIRED_PACKAGES = {
    "requests": "requests",   # downloading
    "pypdf": "pypdf",         # merging PDFs
}


def ensure_packages() -> None:
    missing = []
    for import_name, pip_name in REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append(pip_name)
    if not missing:
        return

    print("This script needs these Python libraries:", ", ".join(missing))
    auto = "-y" in sys.argv or "--yes" in sys.argv
    if not auto:
        if not (sys.stdin and sys.stdin.isatty()):
            sys.exit("Install them with:  pip install " + " ".join(missing))
        if input("Install them now with pip? (Y/n): ").strip().lower() in ("n", "no"):
            sys.exit("Cancelled. You can install them yourself with:  pip install " + " ".join(missing))

    result = subprocess.run([sys.executable, "-m", "pip", "install", *missing],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr[-800:])
        sys.exit("Could not install them automatically. Try:\n"
                 "    python -m venv .venv\n"
                 "    (activate it)  then:  pip install " + " ".join(missing))
    importlib.invalidate_caches()
    print("Installed.\n")


ensure_packages()

import requests                      # noqa: E402  (imported after the install check)
from pypdf import PdfReader, PdfWriter   # noqa: E402

# =====================================================================
# CONFIG: edit this section to customize
# =====================================================================

# --- Questions on start-up ----------------------------------------
# True  = when you run the script it ASKS you what to download (subject, years, sessions,
#         file types, papers, merging...). You can also pick "use the saved settings" there.
# False = skip all questions and just use the settings below.
INTERACTIVE = True

THIS_YEAR = datetime.date.today().year   # "end_year": THIS_YEAR always means "up to now"

# --- Saved subjects (used when INTERACTIVE = False, or you choose "saved settings") ---
# One entry per syllabus code. To add a subject, copy an entry and change it.
# All the pieces come from the subject's address on the site, e.g.
#   https://bestexamhelp.com/exam/cambridge-international-a-level/physics-9702/2023/...
#                                 \________ level _____________/ \_ slug _/
# Levels:  "cambridge-international-a-level"  |  "cambridge-igcse"
#          "cambridge-igcse-9-1"              |  "cambridge-o-level"
# Browse https://bestexamhelp.com/exam/index.php to find a subject's slug and code.
#
#   name        any label you like, used for the folder name
#   start_year / end_year   years to look at (years the site doesn't have are skipped)
#   compile     True = also merge this subject into bookmarked PDFs when done
#               (big subjects like Physics/Maths make HUGE files, so they're off by default)
SUBJECTS = [
    {
        "name": "Computer Science",
        "level": "cambridge-international-a-level",
        "slug": "computer-science-9618",
        "code": "9618",
        "start_year": 2021,      # 2021 had BOTH 9618 (new) and 9608 (old)
        "end_year": THIS_YEAR,
        "compile": True,
    },
    {
        "name": "Computer Science",
        "level": "cambridge-international-a-level",
        "slug": "computer-science-9608",
        "code": "9608",
        "start_year": 2015,
        "end_year": 2021,
        "compile": True,
    },
    {
        "name": "Physics",
        "level": "cambridge-international-a-level",
        "slug": "physics-9702",
        "code": "9702",
        "start_year": 2015,
        "end_year": THIS_YEAR,
        "compile": False,
    },
    {
        "name": "Mathematics",
        "level": "cambridge-international-a-level",
        "slug": "mathematics-9709",
        "code": "9709",
        "start_year": 2015,
        "end_year": THIS_YEAR,
        "compile": False,
    },
]

# --- What to download ----------------------------------------------
# qp = question paper     ms = mark scheme       er = examiner report
# gt = grade threshold    in = insert            sf = source files
# ci = confidential instructions (practical exams, e.g. Physics)
TYPES = ["qp", "ms", "er", "gt"]

# Optional: only certain paper numbers (empty = everything the site lists).
# 31 = exactly paper 31, 3 = every paper 3x (31, 32, 33). Reports/thresholds are always kept.
ONLY_PAPERS = []

# Optional: only these sessions (empty = whatever exists). m = Feb/March, s = May/June, w = Oct/Nov
ONLY_SESSIONS = []

# --- Output --------------------------------------------------------
# Everything is created next to this script file, wherever you launch it from.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKIP_EXISTING = True             # skip files you already have (incomplete ones are re-downloaded)

# --- Speed & network behaviour ------------------------------------
WORKERS = 4                      # files downloaded at the same time (1-8). Higher = faster, but
                                 # more likely to be blocked. If you get blocked, lower this.
DELAY_SECONDS = 0.3              # small pause before each request, per worker
RETRIES = 3                      # tries per file before giving up
RETRY_WAIT = 2                   # seconds before the 2nd try (4s before the 3rd, ...)
MAX_CONSECUTIVE_FAILS = 5        # if this many downloads IN A ROW fail (not counting files
                                 # that simply don't exist on the server), stop early and
                                 # write failed_links.txt so you can finish in your browser

# --- Compiling -----------------------------------------------------
NEWEST_FIRST = True              # True: 2026 -> 2015, False: 2015 -> 2026
OPEN_WHEN_DONE = "files"         # "files" = open compiled PDFs, "folder" = open the folder, "none"
MAX_FILES_PER_COMPILED = 400     # big subjects are split into several merged PDFs of about this
                                 # many files each (keeps memory use and file size sane)

# =====================================================================
# END OF CONFIG: you shouldn't need to edit below this line
# =====================================================================

SITE_DEFAULT = "https://bestexamhelp.com"

# This User-Agent + Referer combination was tested and works on the site.
BASE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
                   "Gecko/20100101 Firefox/128.0"),
}

SESSION_NAMES = {"m": "Feb-March", "s": "May-June", "w": "Oct-Nov"}
SESSION_ORDER = {"m": 0, "s": 1, "w": 2}
TYPE_NAMES = {"qp": "Question Paper", "ms": "Mark Scheme", "er": "Examiner Report",
              "gt": "Grade Threshold", "in": "Insert", "sf": "Source Files",
              "ci": "Confidential Instructions"}
# order inside a session in the compiled PDF (after the paper number): qp, then its ms, ...
TYPE_ORDER = {"qp": 0, "ms": 1, "in": 2, "sf": 3, "ci": 4, "er": 5, "gt": 6}
LEVEL_CODES = {"a-level": "cambridge-international-a-level", "igcse": "cambridge-igcse",
               "igcse-9-1": "cambridge-igcse-9-1", "o-level": "cambridge-o-level"}

PRINT_LOCK = threading.Lock()


def log(msg: str = "") -> None:
    with PRINT_LOCK:
        print(msg, flush=True)


# ---------------------------------------------------------------------
# Settings (one object instead of scattered global variables)
# ---------------------------------------------------------------------
@dataclass
class Settings:
    site: str = SITE_DEFAULT
    types: list = field(default_factory=lambda: list(TYPES))
    only_papers: list = field(default_factory=lambda: list(ONLY_PAPERS))
    only_sessions: list = field(default_factory=lambda: list(ONLY_SESSIONS))
    base_dir: str = SCRIPT_DIR
    skip_existing: bool = SKIP_EXISTING
    workers: int = WORKERS
    delay: float = DELAY_SECONDS
    retries: int = RETRIES
    retry_wait: float = RETRY_WAIT
    max_consecutive_fails: int = MAX_CONSECUTIVE_FAILS
    newest_first: bool = NEWEST_FIRST
    open_when_done: str = OPEN_WHEN_DONE
    max_files_per_compiled: int = MAX_FILES_PER_COMPILED
    assume_yes: bool = False
    dry_run: bool = False

    @property
    def output_dir(self) -> str:
        return os.path.join(self.base_dir, "papers")

    @property
    def compiled_dir(self) -> str:
        return os.path.join(self.base_dir, "compiled version")

    @property
    def home(self) -> str:
        return self.site + "/"


# ---------------------------------------------------------------------
# Networking: reused connections, retries, adaptive slow-down
# ---------------------------------------------------------------------
def pdf_complete(r) -> bool:
    """True only for a complete PDF response (not an HTML error page, not cut off)."""
    if r.status_code != 200:
        return False
    body = r.content
    if b"%PDF" not in body[:1024]:
        return False
    length = r.headers.get("Content-Length")
    if length and length.isdigit() and not r.headers.get("Content-Encoding"):
        return len(body) == int(length)
    return b"%%EOF" in body[-4096:]


def file_ok(path: str) -> bool:
    """True if a file on disk looks like a complete PDF (starts with %PDF, ends with %%EOF)."""
    try:
        size = os.path.getsize(path)
        if size < 200:
            return False
        with open(path, "rb") as f:
            head = f.read(1024)
            f.seek(max(0, size - 4096))
            tail = f.read()
        return b"%PDF" in head and b"%%EOF" in tail
    except OSError:
        return False


class Client:
    """HTTP helper. One connection pool per thread (fast), cookies wiped before every
    request (so nothing sticks between files), and it slows itself down when the site
    starts refusing requests."""

    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.local = threading.local()
        self.slow = 0.0                      # extra seconds added before each request
        self.stop = threading.Event()        # set to cancel everything

    def _session(self):
        s = getattr(self.local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update(BASE_HEADERS)
            self.local.session = s
        return s

    def note_success(self) -> None:
        self.slow = max(0.0, self.slow * 0.7 - 0.05)

    def note_failure(self) -> None:
        self.slow = min(self.slow * 2 + 0.5, 8.0)

    def get(self, url: str, is_ok, referers: list):
        """GET with retries. Returns (response_or_None, error_text).
        A 404 is final ('really doesn't exist') and not retried. Each retry claims a
        different origin, because the site sometimes answers with its homepage if it doesn't
        like where a request 'came from'."""
        cfg = self.cfg
        r, err = None, ""
        for attempt in range(1, cfg.retries + 1):
            if self.stop.is_set():
                return None, "cancelled"
            if self.stop.wait(cfg.delay * (0.7 + 0.6 * random.random()) + self.slow):
                return None, "cancelled"
            ref = referers[(attempt - 1) % len(referers)]
            session = self._session()
            session.cookies.clear()
            try:
                r = session.get(url, headers={"Referer": ref}, timeout=(10, 40))
                err = ""
            except Exception as e:
                r, err = None, f"{type(e).__name__}: {e}"
            if r is not None and (r.status_code == 404 or is_ok(r)):
                return r, ""
            if attempt < cfg.retries and self.stop.wait(cfg.retry_wait * 2 ** (attempt - 1)):
                return None, "cancelled"
        return r, err


def paper_page_of(pdf_url: str) -> str:
    """The web page that links to a PDF, e.g. .../9618_s22_ms_31.pdf -> .../9618-s22-ms-31.php"""
    head, name = pdf_url.rsplit("/", 1)
    return head + "/" + name.replace("_", "-").replace(".pdf", ".php")


# ---------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------
_SMALL_WORDS = {"and", "of", "in", "the"}
_ACRONYMS = {"us": "(US)", "ict": "ICT", "aict": "AICT"}


def pretty_name(base: str) -> str:
    """'english-first-language-us' -> 'English First Language (US)'"""
    words = []
    for i, w in enumerate(base.split("-")):
        if w in _ACRONYMS:
            words.append(_ACRONYMS[w])
        elif i > 0 and w in _SMALL_WORDS:
            words.append(w)
        else:
            words.append(w.capitalize())
    return " ".join(words)


def pdf_name(code: str, sess: str, year: int, typ: str, num) -> str:
    """e.g. 9618_w25_ms_32.pdf  (er/gt have no paper number: 9618_w25_er.pdf)"""
    base = f"{code}_{sess}{year % 100:02d}_{typ}"
    return f"{base}_{num}.pdf" if num else f"{base}.pdf"


def subject_folder(subject: dict) -> str:
    return f"{subject['name']} {subject['code']}"


def subject_years(subject: dict) -> list:
    """The years to process: an explicit list ('years') or the start_year..end_year range."""
    if subject.get("years"):
        return sorted(subject["years"])
    return list(range(subject["start_year"], subject["end_year"] + 1))


def paper_wanted(cfg: Settings, n) -> bool:
    """ONLY_PAPERS filter. 31 = exactly paper 31; 3 = every paper 3x. Reports/thresholds always pass."""
    if n is None or not cfg.only_papers:
        return True
    return n in cfg.only_papers or n // 10 in cfg.only_papers


# ---------------------------------------------------------------------
# Discovery: read each year's listing page to see what really exists
# ---------------------------------------------------------------------
def discover(client: Client, cfg: Settings, subject: dict, year: int):
    """Returns (status, items, detail). status is one of:
       ok          listing loaded; items = [(session, type, paper_number_or_None), ...]
       missing     the site says 404 (year not available)
       unavailable page loaded but it's not the listing (year not on the site, or blocked)
       layout      listing loaded but no links found (the site layout may have changed)
       network     couldn't reach the site at all"""
    base = f"{cfg.site}/exam/{subject['level']}/{subject['slug']}/{year}/"
    marker = f"Year {year}".encode()
    r, err = client.get(base, lambda x: x.status_code == 200 and marker in x.content, [cfg.home, ""])
    if r is None:
        return "network", [], err
    if r.status_code == 404:
        return "missing", [], ""
    if r.status_code != 200 or marker not in r.content:
        return "unavailable", [], f"HTTP {r.status_code}"

    code, yy = subject["code"], f"{year % 100:02d}"
    pattern = re.compile(rf"{code}-([msw])(\d\d)-([a-z]+)(?:-(\d+))?\.php")
    found = set()
    for sess, y2, typ, num in pattern.findall(r.text):
        if y2 == yy:
            found.add((sess, typ, int(num) if num else None))
    if not found:
        return "layout", [], ""
    items = sorted(found, key=lambda t: (SESSION_ORDER[t[0]], t[2] or 999, TYPE_ORDER.get(t[1], 50)))
    return "ok", items, ""


@dataclass
class Task:
    subject: dict
    year: int
    sess: str
    typ: str
    num: Optional[int]
    filename: str
    url: str
    path: str
    have: bool = False


def build_plan(client: Client, cfg: Settings, subjects: list):
    """Look up every subject/year in parallel. Returns (tasks, network_errors), where
    network_errors counts the subject-years whose page couldn't be reached at all."""
    jobs = [(s, y) for s in subjects for y in subject_years(s)]
    log(f"\nChecking the site for what exists ({len(jobs)} subject-years)...")
    results = {}
    with ThreadPoolExecutor(max_workers=max(1, min(cfg.workers, 6))) as ex:
        futures = {ex.submit(discover, client, cfg, s, y): (s["level"], s["slug"], y) for s, y in jobs}
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()

    tasks, network_errors = [], 0
    for s in subjects:
        code = s["code"]
        ok_years = unavailable = 0
        for year in subject_years(s):
            status, items, detail = results[(s["level"], s["slug"], year)]
            if status == "network":
                network_errors += 1
                log(f"[ERROR]   {code} {year}: couldn't reach the site ({detail})")
                continue
            if status == "missing":
                log(f"[SKIP]    {code} {year}: not on the site")
                continue
            if status == "unavailable":
                unavailable += 1
                log(f"[SKIP]    {code} {year}: no listing page ({detail}); year probably not on the site")
                continue
            if status == "layout":
                log(f"[WARN]    {code} {year}: listing loaded but no paper links found "
                    f"- the site layout may have changed")
                continue
            ok_years += 1
            items = [(se, t, n) for (se, t, n) in items
                     if t in cfg.types
                     and (not cfg.only_sessions or se in cfg.only_sessions)
                     and paper_wanted(cfg, n)]
            folder = os.path.join(cfg.output_dir, subject_folder(s), str(year))
            year_tasks = []
            for se, t, n in items:
                filename = pdf_name(code, se, year, t, n)
                path = os.path.join(folder, filename)
                have = cfg.skip_existing and file_ok(path)
                if os.path.exists(path + ".part"):
                    try:
                        os.remove(path + ".part")
                    except OSError:
                        pass
                year_tasks.append(Task(s, year, se, t, n, filename,
                                       f"{cfg.site}/exam/{s['level']}/{s['slug']}/{year}/{filename}",
                                       path, have))
            tasks += year_tasks
            sessions = sorted({t.sess for t in year_tasks}, key=lambda x: SESSION_ORDER[x])
            new = sum(1 for t in year_tasks if not t.have)
            log(f"[FOUND]   {code} {year}: {len(year_tasks)} files ({new} new), sessions: "
                f"{', '.join(SESSION_NAMES[x] for x in sessions) or 'none'}")
        if ok_years == 0 and unavailable > 0:
            log(f"[WARN]    {subject_folder(s)}: no year loaded at all. Either the subject address "
                f"'{s['slug']}' is wrong, or the site is blocking you right now.")
    return tasks, network_errors


# ---------------------------------------------------------------------
# Downloading (parallel, safe writes)
# ---------------------------------------------------------------------
class RunState:
    def __init__(self, total: int):
        self.lock = threading.Lock()
        self.total = total
        self.done = 0
        self.ok = []
        self.missing = []            # listed on the page but 404 on the server
        self.failed = []             # (task, detail)
        self.consecutive = 0
        self.blocked = False


def do_task(client: Client, cfg: Settings, task: Task, state: RunState) -> None:
    if client.stop.is_set():
        return
    r, err = client.get(task.url, pdf_complete, [cfg.home, paper_page_of(task.url), ""])
    if r is None and err == "cancelled":
        return

    label = f"{task.year}/{task.filename}"
    if r is not None and pdf_complete(r):
        tmp = task.path + ".part"
        try:
            os.makedirs(os.path.dirname(task.path), exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(r.content)
            os.replace(tmp, task.path)       # only now does the file "exist"
        except OSError as e:
            # disk full, no permission, or (Windows) the PDF is open in a viewer.
            # Not the site's fault, so it doesn't count towards "blocked".
            try:
                os.remove(tmp)
            except OSError:
                pass
            with state.lock:
                state.done += 1
                state.failed.append((task, f"couldn't save the file ({type(e).__name__}: {e})"))
                n = state.done
            log(f"[{n}/{state.total}] FAILED   {label}: couldn't save the file ({e})")
            return
        client.note_success()
        with state.lock:
            state.done += 1
            state.ok.append(task)
            state.consecutive = 0
            n = state.done
        log(f"[{n}/{state.total}] OK       {label}")
    elif r is not None and r.status_code == 404:
        with state.lock:
            state.done += 1
            state.missing.append(task)
            state.consecutive = 0            # the server answered normally, so not blocked
            n = state.done
        log(f"[{n}/{state.total}] MISSING  {label} (listed, but the server says 404)")
    else:
        detail = (f"no response ({err})" if r is None else
                  f"HTTP {r.status_code}, {r.headers.get('Content-Type', 'unknown')}, {len(r.content)} bytes")
        client.note_failure()
        with state.lock:
            state.done += 1
            state.failed.append((task, detail))
            state.consecutive += 1
            n = state.done
            trip = state.consecutive >= cfg.max_consecutive_fails
            if trip:
                state.blocked = True
        log(f"[{n}/{state.total}] FAILED   {label}: {detail}")
        if trip:
            client.stop.set()


def download_all(client: Client, cfg: Settings, tasks: list):
    """Download every task that isn't on disk yet. Returns (state, interrupted)."""
    need = [t for t in tasks if not t.have]
    state = RunState(len(need))
    if not need:
        return state, False

    log(f"\nDownloading {len(need)} files with {cfg.workers} at a time...  (Ctrl+C stops safely)\n")
    interrupted = False
    ex = ThreadPoolExecutor(max_workers=max(1, cfg.workers))
    futures = [ex.submit(do_task, client, cfg, t, state) for t in need]
    try:
        for fut in as_completed(futures):
            try:
                fut.result()                 # surfaces any unexpected error from a worker
            except Exception as e:
                log(f"[ERROR]   a download crashed unexpectedly: {type(e).__name__}: {e}")
    except KeyboardInterrupt:
        interrupted = True
        client.stop.set()
        log("\nStopping... finishing the files already in progress.")
        for f in futures:
            f.cancel()
    finally:
        ex.shutdown(wait=True, cancel_futures=True)
    return state, interrupted


def write_links_file(cfg: Settings, urls: list, name: str) -> str:
    out = os.path.join(cfg.base_dir, name)
    os.makedirs(cfg.base_dir, exist_ok=True)
    with open(out, "w") as f:
        f.write("\n".join(urls))
    return out


# ---------------------------------------------------------------------
# Compiling (split into several PDFs so memory and file size stay sane)
# ---------------------------------------------------------------------
def collect_files(cfg: Settings, subject: dict) -> list:
    code = subject["code"]
    pattern = re.compile(rf"^{code}_([msw])(\d\d)_([a-z]+)(?:_(\d+))?\.pdf$")
    files = []
    for year in subject_years(subject):
        d = os.path.join(cfg.output_dir, subject_folder(subject), str(year))
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            m = pattern.match(fn)
            if not m:
                continue
            path = os.path.join(d, fn)
            if not file_ok(path):
                log(f"[COMPILE] Skipping incomplete file {fn}")
                continue
            sess, _, typ, num = m.groups()
            files.append((year, sess, typ, int(num) if num else None, path))
    files.sort(key=lambda f: (-f[0] if cfg.newest_first else f[0], SESSION_ORDER[f[1]],
                              f[3] if f[3] is not None else 999, TYPE_ORDER.get(f[2], 50)))
    return files


def write_compiled_part(cfg: Settings, subject: dict, part: list) -> Optional[str]:
    writer = PdfWriter()
    year_items, sess_items, added = {}, {}, 0
    for year, sess, typ, num, path in part:
        first_page = len(writer.pages)
        try:
            writer.append(path)
        except Exception as e:
            log(f"[COMPILE] Skipping unreadable file {os.path.basename(path)}: {e}")
            continue
        if year not in year_items:
            year_items[year] = writer.add_outline_item(str(year), first_page)
        if (year, sess) not in sess_items:
            sess_items[(year, sess)] = writer.add_outline_item(
                SESSION_NAMES[sess], first_page, parent=year_items[year])
        label = TYPE_NAMES.get(typ, typ) if num is None else f"Paper {num} - {TYPE_NAMES.get(typ, typ)}"
        writer.add_outline_item(label, first_page, parent=sess_items[(year, sess)])
        added += 1
    if not added:
        return None

    years = sorted(year_items)
    if len(years) == 1:
        span = str(years[0])
    else:
        span = f"{years[-1]}-{years[0]}" if cfg.newest_first else f"{years[0]}-{years[-1]}"
    os.makedirs(cfg.compiled_dir, exist_ok=True)
    out_path = os.path.join(cfg.compiled_dir, f"Compiled {subject['name']} {subject['code']} ({span}).pdf")
    with open(out_path, "wb") as f:
        writer.write(f)
    pages = len(writer.pages)
    try:
        writer.close()
    except Exception:
        pass
    log(f"[COMPILE] {subject['name']} {subject['code']}: {added} files, {pages} pages -> {out_path}")
    return out_path


def compile_subject(cfg: Settings, subject: dict) -> list:
    """Merge a subject's downloaded PDFs into bookmarked PDFs (Year > Session > file).
    Splits into several PDFs (on year boundaries) if there are too many files."""
    files = collect_files(cfg, subject)
    if not files:
        log(f"[COMPILE] {subject['name']} {subject['code']}: nothing downloaded to compile.")
        return []
    parts, current = [], []
    for _, group in itertools.groupby(files, key=lambda f: f[0]):
        group = list(group)
        if current and len(current) + len(group) > cfg.max_files_per_compiled:
            parts.append(current)
            current = []
        current += group
    if current:
        parts.append(current)
    outputs = [write_compiled_part(cfg, subject, p) for p in parts]
    return [o for o in outputs if o]


def open_path(path: str) -> None:
    """Open a file/folder with the system default app (Windows, macOS, Linux)."""
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.run(["open", path])
        else:
            subprocess.run(["xdg-open", path])
    except Exception as e:
        log(f"[OPEN] Couldn't open {path}: {e}")


# ---------------------------------------------------------------------
# Input parsing helpers
# ---------------------------------------------------------------------
def parse_number_list(text: str, max_n: int, allow_all: bool = True):
    """'1,3' / '2-4' / 'a' (= all) -> sorted list within 1..max_n, or None if invalid."""
    text = text.strip().lower()
    if allow_all and text in ("a", "all", "*"):
        return list(range(1, max_n + 1))
    out = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if re.fullmatch(r"\d+-\d+", part):
            a, b = map(int, part.split("-"))
            out.update(range(min(a, b), max(a, b) + 1))
        elif part.isdigit():
            out.add(int(part))
        else:
            return None
    if not out or any(n < 1 or n > max_n for n in out):
        return None
    return sorted(out)


def parse_years(text: str, lo: int, hi: int):
    """'' = all, '2023', '2019-2024', '2019,2021,2023' -> list of years, or None if invalid."""
    text = text.strip()
    if not text:
        return list(range(lo, hi + 1))
    years = set()
    for part in text.replace(" ", "").split(","):
        m = re.fullmatch(r"(\d{4})-(\d{4})", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            years.update(range(min(a, b), max(a, b) + 1))
        elif re.fullmatch(r"\d{4}", part):
            years.add(int(part))
        else:
            return None
    if not years or min(years) < lo or max(years) > hi:
        return None
    return sorted(years)


def parse_int_csv(text: str):
    """'31,32,3' -> [31, 32, 3]; '' -> []; invalid -> None."""
    text = text.replace(" ", "").strip(",")
    if not text:
        return []
    parts = [p for p in text.split(",") if p]
    return [int(p) for p in parts] if all(p.isdigit() for p in parts) else None


# ---------------------------------------------------------------------
# Interactive questions (asked in this order)
# ---------------------------------------------------------------------
LEVELS = [          # (key, label) like the other menus: the KEY is what gets used in web addresses
    ("cambridge-international-a-level", "A Level / AS Level"),
    ("cambridge-igcse", "IGCSE (includes the 9-1 versions)"),
    ("cambridge-o-level", "O Level"),
]
SESSION_MENU = [("m", "February / March"), ("s", "May / June"), ("w", "October / November")]
FILE_TYPE_MENU = [
    ("qp", "Question papers"),
    ("ms", "Mark schemes"),
    ("er", "Examiner reports"),
    ("gt", "Grade thresholds"),
    ("in", "Inserts"),
    ("sf", "Source files"),
    ("ci", "Confidential instructions (practical exams)"),
]


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    return input(f"{prompt}{suffix}: ").strip() or default


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        a = input(f"{prompt} ({hint}): ").strip().lower()
        if not a:
            return default
        if a in ("y", "yes"):
            return True
        if a in ("n", "no"):
            return False
        print("  Please type y or n.")


def ask_menu(title: str, options: list, default: str, multi: bool = False):
    """Show a numbered menu. Returns the chosen key (or list of keys when multi=True)."""
    print(f"\n{title}")
    for i, (_, label) in enumerate(options, 1):
        print(f"  {i}. {label}")
    hint = "number(s), e.g. 1,3 (or 'a' for all)" if multi else "a number"
    while True:
        nums = parse_number_list(ask(f"Choose {hint}", default), len(options))
        if nums and (multi or len(nums) == 1):
            picked = [options[n - 1][0] for n in nums]
            return picked if multi else picked[0]
        print("  That wasn't a valid choice, try again.")


def parse_subject_list(html: str, level_key: str) -> list:
    """Find subject links on a level's page. The site uses RELATIVE links:
         href="accounting-9706/index.php"                      (same level)
         href="../cambridge-igcse-9-1/accounting-0985/..."      (IGCSE 9-1 subjects)
       and a few subjects link to an oddly named .php page instead of index.php, so any
       'slug-1234/...' link counts. Absolute /exam/<level>/slug-1234/... links work too."""
    pattern = re.compile(r'href="(?:(?:\.\./|/exam/)(cambridge-[a-z0-9-]+)/)?([a-z0-9-]+?)-(\d{4})/[^"]*"')
    seen, subjects = set(), []
    for level, base, code in pattern.findall(html):
        level = level or level_key
        slug = f"{base}-{code}"
        if (level, slug) in seen:
            continue
        seen.add((level, slug))
        name = pretty_name(base) + (" (9-1)" if level.endswith("9-1") else "")
        subjects.append({"name": name, "level": level, "slug": slug, "code": code})
    return subjects


# Built-in copy of the site's subject list, used only if the live list can't be loaded.
FALLBACK_SUBJECTS = {
    "cambridge-international-a-level": [
        "accounting-9706", "applied-information-and-communication-technology-9713", "biology-9700",
        "business-9609", "business-studies-9707", "chemistry-9701", "computer-science-9618",
        "computer-science-9608", "computing-9691", "economics-9708", "history-9489",
        "islamic-studies-9488", "law-9084", "mathematics-9709", "mathematics-further-9231",
        "media-studies-9607", "physics-9702", "psychology-9990", "psychology-9698", "sociology-9699",
    ],
    "cambridge-igcse": [
        "accounting-0452", "arabic-first-language-0508", "art-and-design-0400", "agriculture-0600",
        "biology-0610", "business-studies-0450", "chemistry-0620", "computer-science-0478",
        "chinese-first-language-0509", "drama-0411", "economics-0455", "english-first-language-0500",
        "english-first-language-us-0524", "english-literature-0475", "environmental-management-0680",
        "enterprise-0454", "food-and-nutrition-0648", "french-first-language-0501", "geography-0460",
        "global-perspectives-0457", "history-0470", "islamiyat-0493", "mathematics-0580",
        "mathematics-additional-0606", "mathematics-international-0607", "music-0410",
        "malay-first-language-0696", "physics-0625", "physical-education-0413",
        "religious-studies-0490", "science-combined-0653", "sciences-co-ordinated-0654",
        "sociology-0495", "spanish-first-language-0502", "travel-and-tourism-0471",
    ],
    "cambridge-igcse-9-1": [
        "accounting-0985", "arabic-first-language-7184", "art-and-design-0989", "biology-0970",
        "business-studies-0986", "chemistry-0971", "computer-science-0984", "drama-0994",
        "economics-0987", "english-first-language-0990", "english-literature-0992",
        "geography-0976", "history-0977", "mathematics-0980", "music-0978", "physics-0972",
        "physical-education-0995", "sciences-co-ordinated-0973",
    ],
    "cambridge-o-level": [
        "accounting-7707", "biology-5090", "business-studies-7115", "bengali-3204",
        "bangladesh-studies-7094", "chemistry-5070", "commerce-7100", "computer-science-2210",
        "computer-studies-7010", "economics-2281", "english-language-1123", "mathematics-d-4024",
        "mathematics-additional-4037", "physics-5054", "principles-of-accounts-7110",
        "pakistan-studies-2059", "statistics-4040",
    ],
}


def fallback_subjects(level_key: str) -> list:
    levels = [level_key] + (["cambridge-igcse-9-1"] if level_key == "cambridge-igcse" else [])
    out = []
    for lvl in levels:
        for slug in FALLBACK_SUBJECTS.get(lvl, []):
            m = re.fullmatch(r"([a-z0-9-]+)-(\d{4})", slug)
            out.append({"name": pretty_name(m.group(1)) + (" (9-1)" if lvl.endswith("9-1") else ""),
                        "level": lvl, "slug": slug, "code": m.group(2)})
    return out


def load_subject_list(client: Client, cfg: Settings, level_key: str):
    """Read the site's subject list for a level (IGCSE also lists its 9-1 subjects).
    Returns (subjects, reason_if_it_failed)."""
    # The page may redirect to another .php file; any 200 page that has subject links is fine.
    url = f"{cfg.site}/exam/{level_key}/index.php"
    r, err = client.get(url, lambda x: x.status_code == 200 and bool(parse_subject_list(x.text, level_key)),
                        [cfg.home, ""])
    if r is None:
        return [], f"no response: {err}"
    subjects = parse_subject_list(r.text, level_key) if r.status_code == 200 else []
    if subjects:
        return subjects, ""
    reason = f"HTTP {r.status_code}, {len(r.content)} bytes, no subject links in it"
    if r.status_code == 200:
        try:
            os.makedirs(cfg.base_dir, exist_ok=True)
            dump = os.path.join(cfg.base_dir, "debug_subject_page.html")
            with open(dump, "wb") as f:
                f.write(r.content)
            reason += f"; what the site sent was saved to {dump}"
        except OSError:
            pass
    return [], reason


def choose_subjects(client: Client, cfg: Settings, level_key: str) -> list:
    print("\nLoading the subject list from the site...")
    subjects, reason = load_subject_list(client, cfg, level_key)
    if not subjects:
        print(f"  Couldn't load the live list ({reason}).")
        print("  Using the built-in list instead.")
        subjects = fallback_subjects(level_key)

    shown = subjects
    while True:
        print()
        for i, s in enumerate(subjects, 1):
            if s in shown:
                print(f"  {i:>3}. {s['name']} ({s['code']})")
        a = input("Type subject number(s) (e.g. 5 or 5,9), a word to search (e.g. phys),\n"
                  "or a full address like physics-9702: ").strip()
        m = re.fullmatch(r"([a-z0-9-]+)-(\d{4})", a.lower())
        if m:                                              # typed an address directly
            known = [s for s in subjects if s["slug"] == a.lower()]
            return known or [{"name": pretty_name(m.group(1)), "level": level_key,
                              "slug": a.lower(), "code": m.group(2)}]
        nums = parse_number_list(a, len(subjects), allow_all=False) if a else None
        if nums:
            return [subjects[n - 1] for n in nums]
        if a:
            matches = [s for s in subjects if a.lower() in f"{s['name']} {s['code']}".lower()]
            if matches:
                shown = matches
            else:
                print("  Nothing matched, showing everything again.")
                shown = subjects


def run_questions(client: Client, cfg: Settings):
    """Ask everything, fill in cfg, and return the list of subjects to process
    (or None if the user picked the saved settings)."""
    print("=" * 60)
    print("  Past paper downloader")
    print("=" * 60)

    start = ask_menu("What would you like to do?",
                     [("choose", "Choose what to download now"),
                      ("saved", "Use the saved settings in the script (SUBJECTS list)")], "1")
    if start == "saved":
        return None

    level_key = ask_menu("1) Which level?", LEVELS, "1")

    print("\n2) Which subject?")
    chosen = choose_subjects(client, cfg, level_key)

    this_year = datetime.date.today().year
    print("\n3) Which years?")
    print("   Examples:  2023   |   2019-2024   |   2019,2021,2023")
    while True:
        years = parse_years(ask(f"Years (Enter = all, 2015-{this_year})", ""), 2015, this_year)
        if years:
            break
        print(f"  Please use 4-digit years between 2015 and {this_year}, like 2019-2024.")

    sessions = ask_menu("4) Which exam sessions?", SESSION_MENU, "a", multi=True)
    print("   (Sessions a subject doesn't have are skipped automatically.)")

    types = ask_menu("5) What do you want to download?", FILE_TYPE_MENU, "1,2", multi=True)

    print("\n6) Only certain papers?")
    print("   Examples:  3 = every Paper 3 (31, 32, 33)   |   31,32   |   Enter = all papers")
    while True:
        papers = parse_int_csv(ask("Papers", ""))
        if papers is not None:
            break
        print("  Please type numbers separated by commas, like 31,32 or 3.")
    if papers and ("er" in types or "gt" in types):
        print("   (Examiner reports and grade thresholds are not tied to a paper, so they're always included.)")

    print()
    do_compile = ask_yes_no("7) Merge everything into bookmarked PDFs per subject when finished?", False)
    newest, open_it = True, False
    if do_compile:
        newest = ask_menu("   Order inside the merged PDF?", [("new", "Newest year first"),
                                                              ("old", "Oldest year first")], "1") == "new"
        open_it = ask_yes_no("   Open the merged PDF when done?", True)

    print()
    base = ask("8) Save folder (Enter = next to this script)", "")
    base = os.path.abspath(os.path.expanduser(base)) if base else SCRIPT_DIR

    print("\n" + "-" * 60)
    print("  Your choices:")
    print("   Subjects :", ", ".join(f"{s['name']} {s['code']}" for s in chosen))
    contiguous = years == list(range(years[0], years[-1] + 1))
    print("   Years    :", ", ".join(map(str, years)) if len(years) <= 6
          else f"{years[0]}-{years[-1]}" if contiguous
          else f"{len(years)} years between {years[0]} and {years[-1]}")
    print("   Sessions :", ", ".join(SESSION_NAMES[s] for s in sessions))
    print("   Files    :", ", ".join(TYPE_NAMES[t] for t in types))
    print("   Papers   :", ", ".join(map(str, papers)) if papers else "all")
    print("   Merge    :", "yes" if do_compile else "no")
    print("   Saving to:", os.path.join(base, "papers"))
    print("-" * 60)

    cfg.types = types
    cfg.only_papers = papers
    cfg.only_sessions = [] if len(sessions) == len(SESSION_MENU) else sessions
    cfg.base_dir = base
    cfg.newest_first = newest
    cfg.open_when_done = "files" if open_it else "none"
    return [{**s, "years": years, "compile": do_compile} for s in chosen]


# ---------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Download (and optionally merge) Cambridge past papers from bestexamhelp.com. "
                    "With no options it asks you questions.")
    p.add_argument("--subject", action="append", metavar="SLUG",
                   help="subject address part, e.g. physics-9702 (repeat for several)")
    p.add_argument("--level", choices=sorted(LEVEL_CODES), default="a-level",
                   help="level for --subject (default a-level)")
    p.add_argument("--years", help="e.g. 2023 | 2019-2024 | 2019,2021 (default: 2015 to this year)")
    p.add_argument("--sessions", help="letters from m,s,w e.g. s,w (default: all)")
    p.add_argument("--types", help="from qp,ms,er,gt,in,sf,ci (default: qp,ms,er,gt)")
    p.add_argument("--papers", help="e.g. 31,32 or 3 for every paper 3x (default: all)")
    p.add_argument("--out", help="folder to save into (default: next to the script)")
    p.add_argument("--workers", type=int, help="downloads at the same time, 1-8")
    p.add_argument("--compile", action="store_true", help="merge each subject into bookmarked PDFs")
    p.add_argument("--oldest-first", action="store_true", help="oldest year first in merged PDFs")
    p.add_argument("--saved", action="store_true", help="use the SUBJECTS list inside the script, no questions")
    p.add_argument("--dry-run", action="store_true", help="show the plan and stop, download nothing")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p.add_argument("--self-test", action="store_true", help="run the built-in tests and exit")
    return p


def apply_cli(parser: argparse.ArgumentParser, args, cfg: Settings) -> None:
    if args.workers is not None:
        if not 1 <= args.workers <= 8:
            parser.error("--workers must be between 1 and 8")
        cfg.workers = args.workers
    if args.types:
        types = [t.strip() for t in args.types.split(",") if t.strip()]
        bad = [t for t in types if t not in TYPE_NAMES]
        if bad:
            parser.error(f"unknown --types {bad}; choose from {','.join(TYPE_NAMES)}")
        cfg.types = types
    if args.sessions:
        sess = [s.strip() for s in args.sessions.split(",") if s.strip()]
        if any(s not in SESSION_ORDER for s in sess):
            parser.error("--sessions must be letters from m,s,w")
        cfg.only_sessions = [] if len(set(sess)) == 3 else sess
    if args.papers is not None:
        papers = parse_int_csv(args.papers)
        if papers is None:
            parser.error("--papers must be numbers like 31,32 or 3")
        cfg.only_papers = papers
    if args.out:
        cfg.base_dir = os.path.abspath(os.path.expanduser(args.out))
    if args.oldest_first:
        cfg.newest_first = False
    cfg.assume_yes = args.yes
    cfg.dry_run = args.dry_run


def subjects_from_cli(parser, args) -> list:
    years = None
    if args.years:
        years = parse_years(args.years, 2000, datetime.date.today().year)
        if years is None:
            parser.error("--years must look like 2023, 2019-2024 or 2019,2021")
    out = []
    for slug in args.subject:
        m = re.fullmatch(r"([a-z0-9-]+)-(\d{4})", slug.strip().lower())
        if not m:
            parser.error(f"--subject '{slug}' should look like physics-9702")
        s = {"name": pretty_name(m.group(1)), "level": LEVEL_CODES[args.level],
             "slug": slug.strip().lower(), "code": m.group(2), "compile": args.compile}
        if years:
            s["years"] = years
        else:
            s["start_year"], s["end_year"] = 2015, datetime.date.today().year
        out.append(s)
    return out


# ---------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------
def fmt_minutes(seconds: float) -> str:
    return "under a minute" if seconds < 60 else f"about {round(seconds / 60)} min"


def execute(cfg: Settings, client: Client, subjects: list) -> int:
    tasks, network_errors = build_plan(client, cfg, subjects)
    need = [t for t in tasks if not t.have]
    have = len(tasks) - len(need)

    if not tasks:
        if network_errors:
            log(f"\nCouldn't reach the site for {network_errors} subject-year(s), so nothing could be "
                f"planned. Check your internet connection and try again.")
            return 1
        log("\nNothing to download for these choices.")
        return 0
    if network_errors:
        log(f"\n[WARN]    {network_errors} subject-year(s) couldn't be reached and were left out. "
            f"Run the script again later to pick them up.")

    est = len(need) * (cfg.delay + 0.7) / max(1, cfg.workers)
    log(f"\nPlan: {len(tasks)} files listed | {have} already on disk | {len(need)} to download "
        f"(roughly {fmt_minutes(est)}).")
    log(f"Saving to: {cfg.output_dir}")

    if cfg.dry_run:
        for t in need[:10]:
            log(f"   would download: {t.url}")
        if len(need) > 10:
            log(f"   ... and {len(need) - 10} more")
        log("\nDry run: nothing was downloaded.")
        return 0

    if need and not cfg.assume_yes and sys.stdin and sys.stdin.isatty():
        if not ask_yes_no("\nStart downloading?", True):
            log("Cancelled.")
            return 0

    state, interrupted = download_all(client, cfg, tasks)

    done_ids = {id(t) for t in state.ok} | {id(t) for t in state.missing} | {id(t) for t, _ in state.failed}
    not_attempted = [t for t in need if id(t) not in done_ids]
    log("\n" + "=" * 60)
    log(f"Downloaded: {len(state.ok)} | Already had: {have} | Missing on server (404): "
        f"{len(state.missing)} | Failed: {len(state.failed)} | Not attempted: {len(not_attempted)}")
    log(f"Saved in: {cfg.output_dir}")
    if state.missing:
        log("\nListed on the site but 404 on the server (nothing you can do):")
        for t in state.missing:
            log(f"   {t.year}/{t.filename}")

    retry_urls = [t.url for t, _ in state.failed] + [t.url for t in not_attempted]
    if retry_urls:
        links = write_links_file(cfg, retry_urls, "failed_links.txt")
        log(f"\n{len(retry_urls)} files are not downloaded yet. Their links were saved to:\n   {links}")
        log("Run the script again to retry them, or open those links in your browser.")
        if state.blocked:
            log(f"\n[STOPPED] {cfg.max_consecutive_fails} downloads in a row failed, so it stopped early "
                f"(the site is probably blocking you for a while).")
            log("   Try again later with a lower speed, e.g.  --workers 1  , or finish the list in your browser.")
    if interrupted:
        log("\nStopped by you (Ctrl+C). Everything finished so far is kept.")
        return 130

    targets = [s for s in subjects if s.get("compile")]
    if targets:
        log("\n=== Compiling ===")
        compiled = [p for s in targets for p in compile_subject(cfg, s)]
        if compiled and cfg.open_when_done == "files":
            for p in compiled:
                open_path(p)
        elif compiled and cfg.open_when_done == "folder":
            open_path(cfg.compiled_dir)
    return 1 if state.failed or network_errors else 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        return run_self_test()

    cfg = Settings()
    apply_cli(parser, args, cfg)
    client = Client(cfg)

    try:
        subjects = None
        if args.subject:
            subjects = subjects_from_cli(parser, args)
        elif args.saved or not INTERACTIVE or not (sys.stdin and sys.stdin.isatty()):
            subjects = SUBJECTS
        else:
            try:
                subjects = run_questions(client, cfg)
            except EOFError:
                print("No keyboard input available, using the saved settings in the script.")
            if subjects is None:
                subjects = SUBJECTS

        if args.years and not args.subject:
            years = parse_years(args.years, 2000, datetime.date.today().year)
            if years is None:
                parser.error("--years must look like 2023, 2019-2024 or 2019,2021")
            subjects = [{**s, "years": years} for s in subjects]
        if args.compile and not args.subject:
            subjects = [{**s, "compile": True} for s in subjects]

        return execute(cfg, client, subjects)
    except KeyboardInterrupt:
        client.stop.set()
        print("\nCancelled (Ctrl+C). Files already downloaded are kept; run again to continue.")
        return 130


# ---------------------------------------------------------------------
# Built-in self-test (python download_papers.py --self-test)
# Runs against a tiny fake website on your own computer, no internet needed.
# ---------------------------------------------------------------------
def run_self_test() -> int:
    import http.server
    import shutil
    import tempfile
    import unittest

    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"

    GOOD_PDF = b"%PDF-1.4\n" + b"x" * 600 + b"\n%%EOF\n"

    class Handler(http.server.BaseHTTPRequestHandler):
        routes = {}                      # path -> (status, content_type, body, claimed_length)

        def log_message(self, *a):
            pass

        def do_GET(self):
            status, ctype, body, claimed = self.routes.get(self.path, (404, "text/html", b"nope", None))
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body) if claimed is None else claimed))
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

    def listing(year: int, files: list) -> bytes:
        links = "".join(f'<a href="http://x/exam/lvl/test-1234/{year}/1234-{f}.php">x</a>' for f in files)
        return f"<html><title>Test 1234 Year {year}</title>{links}</html>".encode()

    class SelfTests(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=cls.server.serve_forever, daemon=True).start()
            cls.site = f"http://127.0.0.1:{cls.server.server_address[1]}"
            cls.tmp = tempfile.mkdtemp()

        @classmethod
        def tearDownClass(cls):
            cls.server.shutdown()
            shutil.rmtree(cls.tmp, ignore_errors=True)

        def cfg(self, name, **kw):
            c = Settings(site=self.site, base_dir=os.path.join(self.tmp, name), delay=0,
                         retries=2, retry_wait=0.01, workers=2, max_consecutive_fails=2,
                         assume_yes=True, types=["qp", "ms", "er", "gt"])
            for k, v in kw.items():
                setattr(c, k, v)
            return c

        subject = {"name": "Test", "level": "lvl", "slug": "test-1234", "code": "1234", "years": [2023]}

        def test_parsers(self):
            self.assertEqual(parse_number_list("1,3", 7), [1, 3])
            self.assertEqual(parse_number_list("a", 4), [1, 2, 3, 4])
            self.assertIsNone(parse_number_list("a", 4, allow_all=False))
            self.assertIsNone(parse_number_list("9", 4))
            self.assertEqual(parse_years("2019-2021", 2015, 2026), [2019, 2020, 2021])
            self.assertIsNone(parse_years("1999", 2015, 2026))
            self.assertEqual(parse_int_csv("31, 32"), [31, 32])
            self.assertIsNone(parse_int_csv("3x"))
            self.assertEqual(pretty_name("english-first-language-us"), "English First Language (US)")
            self.assertEqual(pdf_name("9702", "m", 2023, "er", None), "9702_m23_er.pdf")
            cfg = Settings(only_papers=[3, 41])
            self.assertTrue(paper_wanted(cfg, 32) and paper_wanted(cfg, 41) and paper_wanted(cfg, None))
            self.assertFalse(paper_wanted(cfg, 42))

        def test_subject_list_parsing(self):
            # real link shapes copied from the site (relative links, ../ links, odd .php targets)
            html = ('<a href="/exam/cambridge-igcse/index.php">IGCSE</a>'
                    '<a href="accounting-0452/index.php">Accounting</a>'
                    '<a href="../cambridge-igcse-9-1/accounting-0985/index.php">Accounting 9-1</a>'
                    '<a href="physics-0625/ics-notes-vfinal.php">Physics</a>'
                    '<a href="accounting-0452/index.php">again</a>')
            subs = parse_subject_list(html, "cambridge-igcse")
            self.assertEqual([(s["level"], s["slug"]) for s in subs],
                             [("cambridge-igcse", "accounting-0452"),
                              ("cambridge-igcse-9-1", "accounting-0985"),
                              ("cambridge-igcse", "physics-0625")])
            self.assertEqual(subs[1]["name"], "Accounting (9-1)")
            a = parse_subject_list('<a href="/exam/cambridge-o-level/index.php">x</a>'
                                   '<a href="computer-science-2210/index.php">cs</a>', "cambridge-o-level")
            self.assertEqual([s["slug"] for s in a], ["computer-science-2210"])

        def test_fallback_list_matches_site(self):
            counts = {k: len(fallback_subjects(k)) for k in LEVEL_CODES.values()}
            self.assertEqual(counts["cambridge-international-a-level"], 20)
            self.assertEqual(counts["cambridge-igcse"], 53)          # 35 IGCSE + 18 (9-1)
            self.assertEqual(counts["cambridge-o-level"], 17)
            self.assertIn("physics-9702", [s["slug"] for s in fallback_subjects("cambridge-international-a-level")])

        def run_flow(self, routes, answers):
            """Run the question flow with a fake keyboard; returns (subjects, cfg, text printed)."""
            from unittest import mock
            Handler.routes = routes
            cfg = self.cfg("q", base_dir=os.path.join(self.tmp, "q"))
            client = Client(cfg)
            it, printed = iter(answers), []
            with mock.patch("builtins.input", lambda *_: next(it)), \
                    mock.patch("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a)))):
                subjects = run_questions(client, cfg)
            return subjects, cfg, "\n".join(printed)

        def test_question_flow_live_list(self):
            """First question is the level, it shows readable names, and the level KEY (not the
            label) is what's used to load the subject list."""
            routes = {"/exam/cambridge-international-a-level/index.php":
                      (200, "text/html", b'<a href="test-1234/index.php">Test</a>', None)}
            subjects, cfg, shown = self.run_flow(routes, ["1", "1", "1", "2023", "a", "1,2", "", "n",
                                                          os.path.join(self.tmp, "q_save")])
            self.assertIn("A Level / AS Level", shown)
            self.assertEqual(subjects[0]["slug"], "test-1234")
            self.assertEqual(subjects[0]["level"], "cambridge-international-a-level")
            self.assertEqual(subjects[0]["years"], [2023])
            self.assertEqual(cfg.types, ["qp", "ms"])
            self.assertEqual(cfg.base_dir, os.path.join(self.tmp, "q_save"))
            self.assertFalse(subjects[0]["compile"])

        def test_question_flow_fallback_list_and_search(self):
            """Live list unavailable -> built-in list; searching by word works for every level."""
            for level_answer, word, expect_slug, expect_level in (
                    ("1", "phys", "physics-9702", "cambridge-international-a-level"),
                    ("2", "chem", "chemistry-0620", "cambridge-igcse"),
                    ("3", "stat", "statistics-4040", "cambridge-o-level")):
                # the numbers in the list never change when you search, so pick by the real number
                number = [s["slug"] for s in fallback_subjects(expect_level)].index(expect_slug) + 1
                subjects, _, shown = self.run_flow({}, ["1", level_answer, word, str(number), "2023", "a", "1,2", "", "n", ""])
                self.assertEqual(subjects[0]["slug"], expect_slug, shown[-300:])
                self.assertEqual(subjects[0]["level"], expect_level)

        def test_pdf_checks(self):
            class R:
                status_code = 200
                headers = {"Content-Length": "100"}
                content = b"%PDF" + b"x" * 50
            self.assertFalse(pdf_complete(R))                      # cut off
            R.headers = {}
            self.assertFalse(pdf_complete(R))                      # no %%EOF
            R.content = GOOD_PDF
            self.assertTrue(pdf_complete(R))
            p = os.path.join(self.tmp, "t.pdf")
            for data, expect in ((GOOD_PDF, True), (GOOD_PDF[:300], False), (b"<html>" * 100, False)):
                with open(p, "wb") as f:
                    f.write(data)
                self.assertEqual(file_ok(p), expect)

        def test_discover_statuses(self):
            Handler.routes = {
                "/exam/lvl/test-1234/2023/": (200, "text/html", listing(2023, ["s23-qp-11"]), None),
                "/exam/lvl/test-1234/2022/": (200, "text/html", b"<html>Year 2022 nothing here</html>", None),
                "/exam/lvl/test-1234/2021/": (200, "text/html", b"<html>homepage</html>", None),
            }
            cfg = self.cfg("disc")
            client = Client(cfg)
            self.assertEqual(discover(client, cfg, self.subject, 2023)[0:2], ("ok", [("s", "qp", 11)]))
            self.assertEqual(discover(client, cfg, self.subject, 2022)[0], "layout")
            self.assertEqual(discover(client, cfg, self.subject, 2021)[0], "unavailable")
            self.assertEqual(discover(client, cfg, self.subject, 2020)[0], "missing")

        def test_download_safe_and_404_not_a_block(self):
            base = "/exam/lvl/test-1234/2023/"
            Handler.routes = {
                base: (200, "text/html",
                       listing(2023, ["s23-qp-11", "s23-ms-11", "s23-qp-12", "s23-qp-13", "s23-qp-21", "s23-er"]), None),
                base + "1234_s23_qp_11.pdf": (200, "application/pdf", GOOD_PDF, None),
                base + "1234_s23_ms_11.pdf": (200, "application/pdf", GOOD_PDF[:100], len(GOOD_PDF)),  # cut off
                base + "1234_s23_er.pdf": (200, "application/pdf", GOOD_PDF, None),
            }
            cfg = self.cfg("dl")
            client = Client(cfg)
            tasks, _ = build_plan(client, cfg, [self.subject])
            self.assertEqual(len(tasks), 6)
            state, _ = download_all(client, cfg, tasks)
            self.assertEqual(len(state.ok), 2)
            self.assertEqual(len(state.missing), 3)             # three 404s in a row ...
            self.assertFalse(state.blocked)                      # ... must NOT count as a block
            self.assertEqual(len(state.failed), 1)               # the cut-off file
            folder = os.path.join(cfg.output_dir, "Test 1234", "2023")
            self.assertEqual(sorted(os.listdir(folder)), ["1234_s23_er.pdf", "1234_s23_qp_11.pdf"])
            # second run only wants the files still missing, and the plan sees the good ones on disk
            tasks2, _ = build_plan(client, cfg, [self.subject])
            self.assertEqual(sum(1 for t in tasks2 if t.have), 2)

        def test_block_detection_stops_early(self):
            base = "/exam/lvl/test-1234/2023/"
            names = ["s23-qp-11", "s23-qp-12", "s23-qp-13", "s23-qp-21", "s23-qp-22", "s23-qp-23"]
            Handler.routes = {base: (200, "text/html", listing(2023, names), None)}
            for n in names:
                Handler.routes[base + "1234_" + n.replace("-", "_") + ".pdf"] = (200, "text/html", b"blocked page", None)
            cfg = self.cfg("block", workers=1)
            client = Client(cfg)
            tasks, _ = build_plan(client, cfg, [self.subject])
            state, _ = download_all(client, cfg, tasks)
            self.assertTrue(state.blocked)
            self.assertLess(len(state.failed), len(tasks))       # it stopped instead of trying everything
            self.assertEqual(len(state.ok), 0)

        def test_save_error_is_reported_not_lost(self):
            """A file that downloads fine but can't be saved is counted as FAILED (and kept for
            failed_links.txt), doesn't count as 'blocked', and leaves no .part file behind."""
            from unittest import mock
            base = "/exam/lvl/test-1234/2023/"
            Handler.routes = {
                base: (200, "text/html", listing(2023, ["s23-qp-11"]), None),
                base + "1234_s23_qp_11.pdf": (200, "application/pdf", GOOD_PDF, None),
            }
            cfg = self.cfg("saveerr")
            client = Client(cfg)
            tasks, _ = build_plan(client, cfg, [self.subject])
            with mock.patch("os.replace", side_effect=PermissionError("file is open in another program")):
                state, _ = download_all(client, cfg, tasks)
            self.assertEqual(len(state.failed), 1)
            self.assertIn("couldn't save", state.failed[0][1])
            self.assertEqual(state.consecutive, 0)
            self.assertFalse(os.path.exists(tasks[0].path + ".part"))

        def test_unreachable_site_is_an_error(self):
            """If the site can't be reached at all, say so and exit with 1 (not 'nothing to do')."""
            from unittest import mock
            cfg = self.cfg("offline", site="http://127.0.0.1:9", retries=1)   # nothing listens on port 9
            printed = []
            with mock.patch("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a)))):
                code = execute(cfg, Client(cfg), [self.subject])
            text = "\n".join(printed)
            self.assertEqual(code, 1)
            self.assertIn("Couldn't reach the site", text)
            self.assertNotIn("Nothing to download", text)

        def test_404_resets_block_streak(self):
            """fail, 404, fail, 404, ... must never trip the 'blocked' stop."""
            base = "/exam/lvl/test-1234/2023/"
            names = ["s23-qp-11", "s23-qp-12", "s23-qp-13", "s23-qp-21", "s23-qp-22", "s23-qp-23"]
            Handler.routes = {base: (200, "text/html", listing(2023, names), None)}
            for i, n in enumerate(names):
                if i % 2 == 0:                     # every other file answers with an HTML page
                    Handler.routes[base + "1234_" + n.replace("-", "_") + ".pdf"] = (200, "text/html", b"blocked", None)
            cfg = self.cfg("streak", workers=1)    # one at a time, so the order is fixed
            client = Client(cfg)
            tasks, _ = build_plan(client, cfg, [self.subject])
            state, _ = download_all(client, cfg, tasks)
            self.assertFalse(state.blocked)
            self.assertEqual((len(state.failed), len(state.missing)), (3, 3))

        def test_saved_settings_choice(self):
            """Picking 'saved settings' on the first question returns None (main then uses SUBJECTS)."""
            from unittest import mock
            cfg = self.cfg("saved")
            with mock.patch("builtins.input", lambda *_: "2"), mock.patch("builtins.print", lambda *a, **k: None):
                self.assertIsNone(run_questions(Client(cfg), cfg))

        def test_years_respect_lower_limit(self):
            self.assertIsNone(parse_years("2010", 2015, 2026))
            self.assertEqual(parse_years("2010", 2000, 2026), [2010])

        def test_compile_splits_and_bookmarks(self):
            cfg = self.cfg("comp", max_files_per_compiled=2)
            subject = {"name": "Test", "level": "lvl", "slug": "test-1234", "code": "1234", "years": [2021, 2022, 2023]}
            for year, names in ((2023, ["s23_qp_11", "s23_ms_11"]), (2022, ["s22_qp_11", "s22_ms_11"]), (2021, ["w21_qp_11"])):
                d = os.path.join(cfg.output_dir, "Test 1234", str(year))
                os.makedirs(d, exist_ok=True)
                for n in names:
                    w = PdfWriter()
                    w.add_blank_page(72, 72)
                    with open(os.path.join(d, f"1234_{n}.pdf"), "wb") as f:
                        w.write(f)
            outs = compile_subject(cfg, subject)
            self.assertEqual(len(outs), 3)
            self.assertEqual(len(PdfReader(outs[0]).pages), 2)
            self.assertTrue(len(PdfReader(outs[0]).outline) >= 1)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SelfTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    exit_code = 1
    try:
        exit_code = main()
    finally:
        # keep the window open when the script was double-clicked on Windows
        if os.name == "nt" and len(sys.argv) == 1 and sys.stdin and sys.stdin.isatty():
            try:
                input("\nPress Enter to close...")
            except EOFError:
                pass
    sys.exit(exit_code)
