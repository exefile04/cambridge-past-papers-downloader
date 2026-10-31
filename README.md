# Cambridge Past Papers Downloader

Download Cambridge (CAIE) past papers, mark schemes, examiner reports and grade thresholds from [bestexamhelp.com](https://bestexamhelp.com) in one go, neatly sorted into folders. It can also merge everything into **bookmarked PDFs** (Year → Session → Paper) so you can flip through years of papers in a single file.

Works for **A Level / AS Level, IGCSE (including 9-1) and O Level**, on Windows, macOS and Linux.

---

## Features

- **Interactive mode**: just run it and answer a few questions (level, subject, years, sessions, file types, papers)
- **Reads the site first**: checks what actually exists for each year before downloading, then shows you a plan and asks before starting
- **Fast and polite**: downloads a few files at a time with small pauses, and slows itself down if the site starts refusing requests
- **Safe downloads**: files are never left half-written; incomplete or corrupt files are re-downloaded on the next run
- **Resumable**: files you already have are skipped, so you can stop with Ctrl+C and continue later
- **Bookmarked PDF compiler**: merges a subject into one or more PDFs with a clickable table of contents
- **Filters**: pick specific sessions (Feb/March, May/June, Oct/Nov) or papers (e.g. only Paper 3)
- **Failed links list**: anything that couldn't be downloaded is saved to `failed_links.txt` so you can retry or open it in a browser
- **Built-in tests** that run offline

## Requirements

- Python **3.9 or newer**
- The libraries `requests` and `pypdf` (the script offers to install them for you on first run)

To install them yourself:

```bash
pip install requests pypdf
```

## Quick start

```bash
git clone https://github.com/<your-username>/cambridge-past-papers-downloader.git
cd cambridge-past-papers-downloader
python download_papers.py
```

On Windows you can also just double-click `download_papers.py`.

The script will ask you:

1. Level (A Level, IGCSE, O Level)
2. Subject (pick a number, search by word like `phys`, or type an address like `physics-9702`)
3. Years (e.g. `2023`, `2019-2024`, `2019,2021,2023`)
4. Exam sessions
5. File types (question papers, mark schemes, …)
6. Specific papers (optional)
7. Whether to merge into bookmarked PDFs
8. Where to save

## Command-line usage

Skip the questions by passing options directly:

```bash
# Physics 9702, 2019-2024, question papers and mark schemes, no confirmation prompt
python download_papers.py --subject physics-9702 --years 2019-2024 --types qp,ms --yes

# Only Paper 3 (31, 32, 33), May/June and Oct/Nov, then merge into a PDF
python download_papers.py --subject mathematics-9709 --papers 3 --sessions s,w --compile

# IGCSE subject
python download_papers.py --level igcse --subject chemistry-0620 --years 2022-2025

# Use the SUBJECTS list saved inside the script
python download_papers.py --saved

# See what WOULD be downloaded, without downloading anything
python download_papers.py --saved --dry-run

# Run the built-in tests (no internet needed)
python download_papers.py --self-test
```

| Option | What it does |
|---|---|
| `--subject SLUG` | Subject address, e.g. `physics-9702` (repeat for several) |
| `--level` | `a-level` (default), `igcse`, `igcse-9-1`, `o-level` |
| `--years` | `2023`, `2019-2024` or `2019,2021` |
| `--sessions` | Any of `m` (Feb/March), `s` (May/June), `w` (Oct/Nov) |
| `--types` | Any of `qp,ms,er,gt,in,sf,ci` (default `qp,ms,er,gt`) |
| `--papers` | `31,32` for exact papers, `3` for every Paper 3x |
| `--out FOLDER` | Where to save (default: next to the script) |
| `--workers N` | Downloads at the same time, 1–8 (default 4) |
| `--compile` | Merge each subject into bookmarked PDFs |
| `--oldest-first` | Oldest year first in merged PDFs |
| `--saved` | Use the `SUBJECTS` list in the script |
| `--dry-run` | Show the plan only |
| `-y`, `--yes` | Don't ask for confirmation |
| `--self-test` | Run the built-in tests |

### File type codes

| Code | Meaning |
|---|---|
| `qp` | Question paper |
| `ms` | Mark scheme |
| `er` | Examiner report |
| `gt` | Grade threshold |
| `in` | Insert |
| `sf` | Source files |
| `ci` | Confidential instructions (practical exams) |

## Finding a subject's address

Open the subject on the site and look at the URL:

```
https://bestexamhelp.com/exam/cambridge-international-a-level/physics-9702/2023/
                              \____________ level ____________/ \__ slug __/
```

The **slug** (`physics-9702`) is what you pass to `--subject`.

## Output

```
papers/
└── Physics 9702/
    ├── 2023/
    │   ├── 9702_s23_qp_11.pdf
    │   ├── 9702_s23_ms_11.pdf
    │   └── ...
    └── 2024/
compiled version/
└── Compiled Physics 9702 (2024-2019).pdf
failed_links.txt        (only if something failed)
```

Large subjects are split into several merged PDFs (about 400 files each) to keep file sizes manageable.

## Saved settings

Open `download_papers.py` and edit the **CONFIG** section at the top to set your default subjects, file types, speed and more. Set `INTERACTIVE = False` to skip the questions entirely.

## Troubleshooting

- **"Stopped early … the site is probably blocking you"**: wait a while, then run again with `--workers 1`. Already-downloaded files are skipped.
- **"Couldn't reach the site"**: check your internet connection.
- **"Couldn't save the file"**: make sure the PDF isn't open in another program and that you have space and permission in the save folder.
- **Pip install fails** (common on newer Linux/macOS): create a virtual environment first:
  ```bash
  python -m venv .venv
  source .venv/bin/activate      # Windows: .venv\Scripts\activate
  pip install requests pypdf
  ```

## Please be kind to the site

bestexamhelp.com is a free educational resource. Keep the number of workers low, don't re-run huge downloads unnecessarily, and check the site's terms of use. This project is not affiliated with bestexamhelp.com or Cambridge University Press & Assessment. Past papers remain the property of their respective owners.

## License

MIT (see `LICENSE`)
