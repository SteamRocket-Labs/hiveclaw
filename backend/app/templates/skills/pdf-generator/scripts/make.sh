#!/usr/bin/env bash
# make.sh -- Orchestrator for the PDF generation pipeline.
#
# Usage:
#   bash scripts/make.sh run --title "Report" --type report --author "Team" --date "2025" \
#       --accent "#2D5F8A" --content content.json --out report.pdf
#
#   bash scripts/make.sh reformat --input source.md --title "Report" --type report --out output.pdf
#
#   bash scripts/make.sh check    # verify dependencies
#   bash scripts/make.sh demo     # build a sample PDF

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ---- Dependency check ----
check_deps() {
    local ok=true
    for pkg in reportlab pypdf pdfplumber PIL; do
        if ! python3 -c "import $pkg" 2>/dev/null; then
            echo "MISSING: $pkg"
            ok=false
        fi
    done
    if $ok; then
        echo "All dependencies OK"
    else
        echo "NOT READY -- install missing packages"
        exit 1
    fi
}

# ---- Run full CREATE pipeline ----
run_create() {
    local title="" doc_type="general" author="" date_str="" accent="" cover_bg=""
    local content="" output="output.pdf" abstract="" cover_image=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --title) title="$2"; shift 2 ;;
            --type) doc_type="$2"; shift 2 ;;
            --author) author="$2"; shift 2 ;;
            --date) date_str="$2"; shift 2 ;;
            --accent) accent="$2"; shift 2 ;;
            --cover-bg) cover_bg="$2"; shift 2 ;;
            --content) content="$2"; shift 2 ;;
            --out) output="$2"; shift 2 ;;
            --abstract) abstract="$2"; shift 2 ;;
            --cover-image) cover_image="$2"; shift 2 ;;
            *) echo "Unknown option: $1"; exit 1 ;;
        esac
    done

    if [[ -z "$content" ]]; then
        echo "Error: --content is required"
        exit 1
    fi

    local tmpdir
    tmpdir=$(mktemp -d)
    local cover_pdf="$tmpdir/cover.pdf"
    local body_pdf="$tmpdir/body.pdf"

    echo "[1/3] Rendering cover..."
    local cover_args=(--type "$doc_type" --title "$title" --out "$cover_pdf")
    [[ -n "$author" ]] && cover_args+=(--author "$author")
    [[ -n "$date_str" ]] && cover_args+=(--date "$date_str")
    [[ -n "$accent" ]] && cover_args+=(--accent "$accent")
    [[ -n "$cover_bg" ]] && cover_args+=(--cover-bg "$cover_bg")
    [[ -n "$abstract" ]] && cover_args+=(--abstract "$abstract")
    [[ -n "$cover_image" ]] && cover_args+=(--cover-image "$cover_image")
    python3 "$SCRIPT_DIR/render_cover.py" "${cover_args[@]}"

    echo "[2/3] Rendering body..."
    local body_args=(--content "$content" --type "$doc_type" --out "$body_pdf")
    [[ -n "$accent" ]] && body_args+=(--accent "$accent")
    python3 "$SCRIPT_DIR/render_body.py" "${body_args[@]}"

    echo "[3/3] Merging..."
    python3 "$SCRIPT_DIR/merge.py" "$cover_pdf" "$body_pdf" --out "$output"

    rm -rf "$tmpdir"
    echo "Done: $output"
}

# ---- Reformat: parse input -> content.json -> CREATE ----
run_reformat() {
    local input="" title="" doc_type="general" output="output.pdf" accent=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --input) input="$2"; shift 2 ;;
            --title) title="$2"; shift 2 ;;
            --type) doc_type="$2"; shift 2 ;;
            --out) output="$2"; shift 2 ;;
            --accent) accent="$2"; shift 2 ;;
            *) echo "Unknown option: $1"; exit 1 ;;
        esac
    done

    if [[ -z "$input" ]]; then
        echo "Error: --input is required"
        exit 1
    fi

    local tmpdir
    tmpdir=$(mktemp -d)
    local content_json="$tmpdir/content.json"

    echo "Parsing $input..."
    python3 -c "
import json, sys, os

input_path = '$input'
ext = os.path.splitext(input_path)[1].lower()
blocks = []

if ext == '.json':
    with open(input_path) as f:
        blocks = json.load(f)
elif ext in ('.md', '.txt'):
    with open(input_path) as f:
        for line in f:
            line = line.rstrip()
            if line.startswith('### '):
                blocks.append({'type': 'h3', 'text': line[4:]})
            elif line.startswith('## '):
                blocks.append({'type': 'h2', 'text': line[3:]})
            elif line.startswith('# '):
                blocks.append({'type': 'h1', 'text': line[2:]})
            elif line.startswith('- ') or line.startswith('* '):
                blocks.append({'type': 'bullet', 'text': line[2:]})
            elif line.strip():
                blocks.append({'type': 'body', 'text': line})
elif ext == '.pdf':
    import pdfplumber
    with pdfplumber.open(input_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                for para in text.split('\n\n'):
                    para = para.strip()
                    if para:
                        blocks.append({'type': 'body', 'text': para})

with open('$content_json', 'w') as f:
    json.dump(blocks, f, ensure_ascii=False, indent=2)
print(f'Parsed {len(blocks)} blocks')
"

    local create_args=(--title "${title:-Untitled}" --type "$doc_type" --content "$content_json" --out "$output")
    [[ -n "$accent" ]] && create_args+=(--accent "$accent")
    run_create "${create_args[@]}"

    rm -rf "$tmpdir"
}

# ---- Demo ----
run_demo() {
    local tmpdir
    tmpdir=$(mktemp -d)
    cat > "$tmpdir/demo.json" <<'DEMO_EOF'
[
  {"type": "h1", "text": "Introduction"},
  {"type": "body", "text": "This is a demonstration of the PDF generation pipeline. It showcases the design system including typography, color tokens, and page layout."},
  {"type": "callout", "text": "This is a callout block with accent-colored left border, useful for highlighting key insights."},
  {"type": "h2", "text": "Data Overview"},
  {"type": "table", "headers": ["Quarter", "Revenue", "Growth"], "rows": [["Q1", "$120K", "12%"], ["Q2", "$145K", "21%"], ["Q3", "$132K", "-9%"], ["Q4", "$178K", "35%"]]},
  {"type": "h2", "text": "Analysis"},
  {"type": "body", "text": "The quarterly data shows strong overall growth with a temporary dip in Q3. The recovery in Q4 suggests underlying demand remains robust."},
  {"type": "bullet", "text": "Revenue grew 48% year-over-year"},
  {"type": "bullet", "text": "Q4 was the strongest quarter on record"},
  {"type": "bullet", "text": "Growth is expected to continue into next year"},
  {"type": "divider"},
  {"type": "body", "text": "For additional details, refer to the appendix."}
]
DEMO_EOF

    run_create --title "Demo Report" --type report --author "PDF Generator" \
        --date "2025" --accent "#2D5F8A" --content "$tmpdir/demo.json" --out demo.pdf
    rm -rf "$tmpdir"
}

# ---- Main dispatcher ----
case "${1:-help}" in
    run) shift; run_create "$@" ;;
    reformat) shift; run_reformat "$@" ;;
    check) check_deps ;;
    demo) run_demo ;;
    *)
        echo "Usage: make.sh {run|reformat|check|demo} [options]"
        echo ""
        echo "  run       -- Full CREATE pipeline"
        echo "  reformat  -- Parse input document, then CREATE"
        echo "  check     -- Verify dependencies"
        echo "  demo      -- Build a sample PDF"
        ;;
esac
