# Mamadoc

[![PyPI](https://img.shields.io/pypi/v/mamadoc)](https://pypi.org/project/mamadoc/)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.18626396.svg)](https://doi.org/10.5281/zenodo.18626396)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

AI-powered document processing for managing household paperwork. Scan letters as PDFs, get structured extraction with actionable recommendations, and track everything in a Streamlit dashboard.

Started because my mother's paperwork kept piling up — German letters about insurance, care facilities, invoices — and I needed something that could read them, tell me what to do, and track what was still open. The name stuck.

Built for the real-world problem of managing a pile of unsorted letters — insurance notices, invoices, government correspondence, care facility paperwork — where you need to know what each letter says, what you need to do, and which letters belong to the same matter.

## What It Does

- **Multi-language PDF reading** — Claude Vision reads scanned documents in any language (German, Turkish, French, etc.) and produces English summaries
- **Structured extraction** — sender, date, amount, deadline, urgency, document type, reference numbers
- **Actionable recommendations** — each document gets specific action items with deadlines
- **Issue timeline grouping** — automatically groups related documents (original letter → reminder → final notice) so you respond to the latest, not the first one you scanned
- **Personal task manager** — add your own tasks alongside document-extracted actions
- **Conversational Ask tab** — ask questions about your documents and tasks in natural language
- **File watcher** — auto-processes new PDFs dropped into the folder

## How It Works

```
Scanned PDF → Claude Vision API → Structured JSON → SQLite → Streamlit Dashboard
```

1. Scan a letter as PDF (any scanner, any language)
2. Drop it into the project folder
3. Claude Vision reads the image, extracts structured data, generates English summary + recommendations
4. Documents are automatically grouped into issues by sender + reference number
5. Track action items, mark them done, ask questions about your documents

## Requirements

- Python 3.10+
- [Poppler](https://poppler.freedesktop.org/) (for PDF to image conversion)
- [Anthropic API key](https://console.anthropic.com/) (~$0.01 per page)

### Installing Poppler

**Windows:** Download from [poppler releases](https://github.com/oschwartz10612/poppler-windows/releases), extract, add `bin/` to PATH.

**macOS:** `brew install poppler`

**Linux:** `sudo apt install poppler-utils`

## Install

### From PyPI
```bash
pip install mamadoc
```

### From source
```bash
git clone https://github.com/capraCoder/mamadoc.git
cd mamadoc
pip install -e .
```

### Configure
```bash
cp .env.example .env
# Edit .env and add your Anthropic API key
```

Verify setup:
```bash
mamadoc check
```

## Usage

### CLI commands
```bash
# Check environment setup
mamadoc check

# Process all unprocessed PDFs in the project folder
mamadoc process

# Reprocess a specific document
mamadoc process Document_2026-01-15.pdf --force

# Launch the Streamlit dashboard
mamadoc dashboard

# Watch folder for new PDFs and auto-process
mamadoc watch
```

### Direct module execution
```bash
python -m mamadoc.process_pdf
python -m mamadoc.process_pdf Document_2026-01-15.pdf --force
python -m streamlit run mamadoc/app.py
python -m mamadoc.watcher
```

## Dashboard Tabs

| Tab | Purpose |
|-----|---------|
| **Dashboard** | Overview metrics, document table with inline status editing |
| **Issues** | Grouped timelines per matter (original → reminder → final notice) |
| **Document Detail** | Full extraction, page image, action items, re-extract/delete |
| **Pending Actions** | Personal tasks + document-extracted actions with deadlines |
| **Ask** | Natural language questions about your documents and tasks |

## Architecture

```
mamadoc/
  mamadoc/
    __init__.py     — version
    cli.py          — CLI entry point (mamadoc command)
    config.py       — paths, API keys, logging, constants
    prompt.py       — Claude Vision prompts, JSON parsing, validation
    db.py           — SQLite schema + CRUD (documents, actions, issues, tasks)
    process_pdf.py  — PDF → image → Claude Vision → JSON → DB pipeline
    app.py          — Streamlit dashboard (5 tabs)
    watcher.py      — watchdog auto-processor for new PDFs
  .env              — your API key (not committed)
  mamadoc.db        — SQLite database (auto-created)
  processed/        — extracted JSON + page images
  pyproject.toml    — PyPI package config
```

## Cost

Claude Vision API costs approximately **$0.01 per page**. A typical single-page letter costs about 1 cent to process. The issue-linking step uses the cheaper Haiku model.

## Limitations

- PDFs must be scanned images (not born-digital text PDFs — though those work too)
- Maximum 20 pages per PDF (configurable in `config.py`)
- Interface is English-only (PDF reading works in any language)
- No multi-user support — single SQLite database, designed for personal use
- Requires internet connection for Claude API calls

## License

MIT License. See [LICENSE](LICENSE).

## Author

[Kafkas M. Caprazli](https://orcid.org/0000-0002-5744-8944)
