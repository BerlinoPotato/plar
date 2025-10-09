from pathlib import Path
import argparse
from PyPDF2 import PdfReader, PdfWriter

def parse_page_spec(spec: str, num_pages: int):
    """
    spec: 'all' | '1-end' | '1-3,5,7-9'
    Returns 0-based page indices (sorted, unique).
    """
    if not spec or spec.strip().lower() in ("all", "1-end", "end"):
        return list(range(num_pages))

    out = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = [x.strip() for x in part.split("-", 1)]
            a, b = int(a), int(b)
            if a > b:
                a, b = b, a
            a = max(1, a)
            b = min(num_pages, b)   # clamp to last page
            out.update(range(a - 1, b))  # inclusive of b
        else:
            i = int(part)
            if not (1 <= i <= num_pages):
                raise ValueError(f"Page {i} out of range 1..{num_pages}")
            out.add(i - 1)
    return sorted(out)

def combine_pdfs_with_ranges(pdf_inputs, output_path):
    writer = PdfWriter()
    for item in pdf_inputs:
        path = Path(item["path"])
        if not path.exists():
            raise FileNotFoundError(f"PDF not found: {path}")
        reader = PdfReader(str(path))
        sel = parse_page_spec(item.get("type", "all"), len(reader.pages))
        for p in sel:
            writer.add_page(reader.pages[p])

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as f:
        writer.write(f)
    print(f"Combined PDF saved to: {out}")

def cli_combinepdfs():
    p = argparse.ArgumentParser(description="Combine up to two PDFs with page selection.")
    p.add_argument("--pdf1", required=True, help="First PDF file")
    p.add_argument("--type1", default="all", help="Page selection for PDF1: 'all' or '1-3,5'")
    p.add_argument("--pdf2", help="Second PDF file (optional)")
    p.add_argument("--type2", default="all", help="Page selection for PDF2: 'all' or '2,7-12'")
    p.add_argument("--output", required=True, help="Output PDF path")
    args = p.parse_args()

    if args.pdf1:
        
        inputs = [{"path": args.pdf1, "type": (args.type1 or "all").strip() or "all"}]
        # only add second if non-empty after stripping quotes/spaces
        if args.pdf2 and args.pdf2.strip().strip('"'):
            inputs.append({"path": args.pdf2, "type": (args.type2 or "all").strip() or "all"})

        combine_pdfs_with_ranges(inputs, args.output)
    else:
        print('PDF file (First PDF) is mandatory')

if __name__ == "__main__":
    cli_combinepdfs()
