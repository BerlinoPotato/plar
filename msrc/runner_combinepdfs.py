
import argparse, re, sys

from datetime   import datetime
from pathlib    import Path
from PyPDF2     import PdfReader, PdfWriter

#----------------------------------------------------------------------------
AB = ("A", "B")
#----------------------------------------------------------------------------
# ---------- helpers for page math (1-based) ----------
def _parse_int_or_end(tok: str, num_pages: int) -> int:
    """Return 1-based page number from token (int or 'end'), clamped to [1..num_pages]."""
    tok = tok.strip().lower()
    if tok == "end":
        return num_pages
    i = int(tok)
    if i < 1:
        i = 1
    if i > num_pages:
        i = num_pages
    return i

#----------------------------------------------------------------------------
def _expand_range(a: int, b: int, step: int) -> list[int]:
    """Expand inclusive 1-based range a..b with positive step; auto direction."""
    if step <= 0:
        raise ValueError("Step must be a positive integer")
    if a <= b:
        return list(range(a, b + 1, step))
    else:
        return list(range(a, b - 1, -step))



#----------------------------------------------------------------------------
def _split_top_level(seq: str) -> list[str]:
    """Split by commas/whitespace not inside parentheses."""
    parts, buf, depth = [], [], 0
    # normalize whitespace to single spaces; allow commas or spaces as separators
    seq = re.sub(r"\s+", " ", seq.strip())
    i = 0
    while i < len(seq):
        ch = seq[i]
        if ch == "," and depth == 0:
            if buf:
                parts.append("".join(buf).strip())
                buf = []
            i += 1
            continue
        if ch == " " and depth == 0:
            if buf:
                parts.append("".join(buf).strip())
                buf = []
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        buf.append(ch)
        i += 1
    if buf:
        parts.append("".join(buf).strip())
    return [p for p in parts if p]

#----------------------------------------------------------------------------
def _tokenize_compact(body: str) -> list[str]:
    """
    Break compact strings like 'A1B2A10' or 'A1-10:2B5' into atomic pieces:
      - Parenthesized blocks kept as-is
      - Otherwise sequences like A<number>[-<number>][:step]
    """
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        if body[i] == "(":
            # capture whole parenthesized block (may nest)
            depth, j = 1, i + 1
            while j < n and depth > 0:
                if body[j] == "(":
                    depth += 1
                elif body[j] == ")":
                    depth -= 1
                j += 1
            if depth != 0:
                raise ValueError("Unbalanced parentheses in pattern.")
            out.append(body[i:j])  # includes parentheses
            i = j
            # consume optional xN right after
            m = re.match(r"x\s*\d+", body[i:])
            if m:
                out.append(m.group(0))
                i += len(m.group(0))
            continue

        # Expect 'A' or 'B'
        if body[i].upper() not in AB:
            raise ValueError(f"Expected 'A' or 'B' at: {body[i: i+10]}")
        src = body[i].upper()
        i += 1

        # number or 'end'
        m = re.match(r"(end|\d+)", body[i:], flags=re.I)
        if not m:
            raise ValueError(f"Missing page number after {src} at: {body[i: i+10]}")
        n1 = m.group(1)
        i += len(m.group(1))

        # optional range -N2
        n2 = None
        if i < n and body[i] == "-":
            i += 1
            m2 = re.match(r"(end|\d+)", body[i:], flags=re.I)
            if not m2:
                raise ValueError("Bad range end after '-'")
            n2 = m2.group(1)
            i += len(m2.group(1))

        # optional :step
        step = None
        if i < n and body[i] == ":":
            i += 1
            m3 = re.match(r"\d+", body[i:])
            if not m3:
                raise ValueError("Bad step after ':'")
            step = int(m3.group(0))
            i += len(m3.group(0))

        # Build atom string back (normalized)
        atom = f"{src}{n1}"
        if n2 is not None:
            atom += f"-{n2}"
        if step is not None:
            atom += f":{step}"
        out.append(atom)
    return out

#----------------------------------------------------------------------------
def _expand_pattern(pattern: str, pagesA: int, pagesB: int) -> list[tuple[str, int]]:
    
    lv_Pattern      = ''
    iCount          = 0
    lv_patternOrg   = pattern.upper().replace(' ', '')
    lv_patternList  = lv_patternOrg.split(',')
    
    print('To Combine with following pattern...')
    for tp_pttrn in lv_patternList:
        iCount +=1
        if tp_pttrn == 'A':
            lv_pttrn_fix = f'A1-{pagesA}'
        elif tp_pttrn == 'B':
            lv_pttrn_fix = f'B1-{pagesB}'
        else:
            lv_pttrn_fix = tp_pttrn
        
        print(f'{iCount} : {lv_pttrn_fix}')
        lv_Pattern = f'{lv_Pattern},{lv_pttrn_fix}'
    
    lv_Pattern_Final = lv_Pattern.strip(',')
    
    if not lv_Pattern_Final or not lv_Pattern_Final.strip():
        raise ValueError("Pattern must not be empty.")

    chunks = _split_top_level(lv_Pattern_Final)
    
#----------------------------------------------------------------------------
    def expand_chunk(chunk: str) -> list[tuple[str, int]]:
        # Repetition?
        mrep = re.fullmatch(r"\((?P<body>.+)\)\s*x\s*(?P<rep>\d+)", chunk, flags=re.I)
        if mrep:
            body = mrep.group("body").strip()
            rep = int(mrep.group("rep"))
            seq = expand_sequence(body)
            return seq * rep
        # Or a plain parenthesis without immediate xN: expand inner, no repeat
        if chunk.startswith("(") and chunk.endswith(")"):
            return expand_sequence(chunk[1:-1].strip())
        # Otherwise a compact run (e.g., A1B2A10) or a single atom
        return expand_sequence(chunk)

#----------------------------------------------------------------------------
    def expand_sequence(body: str) -> list[tuple[str, int]]:
        # Split compact body into atomic tokens and/or nested (... )xN
        tokens = _tokenize_compact(body)
        out: list[tuple[str, int]] = []
        i = 0
        while i < len(tokens):
            t = tokens[i]
            # Handle "(...)" followed by "xN" if tokens were split that way
            if t.startswith("(") and t.endswith(")"):
                rep = 1
                if i + 1 < len(tokens) and re.fullmatch(r"x\s*\d+", tokens[i + 1], flags=re.I):
                    rep = int(re.findall(r"\d+", tokens[i + 1])[0])
                    i += 1
                inner = t[1:-1].strip()
                out.extend(expand_sequence(inner) * rep)
                i += 1
                continue

            # Atomic page or range token: A<N>[ - <N2> ][ :step ]
            m = re.fullmatch(r"([ABab])(end|\d+)(?:-(end|\d+))?(?::(\d+))?", t)
            if not m:
                raise ValueError(f"Bad token in pattern: {t}")
            src = m.group(1).upper()
            n1s = m.group(2)
            n2s = m.group(3)
            steps = m.group(4)
            step = int(steps) if steps else 1
            n_pages = pagesA if src == "A" else pagesB
            n1 = _parse_int_or_end(n1s, n_pages)
            if n2s:
                n2 = _parse_int_or_end(n2s, n_pages)
                seq_1based = _expand_range(n1, n2, step)
            else:
                seq_1based = [n1]
            # Convert 1-based to zero-based index
            for p1 in seq_1based:
                out.append((src, p1 - 1))
            i += 1
        return out

    plan: list[tuple[str, int]] = []
    for c in chunks:
        plan.extend(expand_chunk(c))
        
    return plan


# ---------- Flexible MIX ----------
#----------------------------------------------------------------------------
def mix_two_pdfs_single_pattern(pdfA: Path, pdfB: Path, pattern: str, output_path: str) -> Path:
    if not pdfA.exists(): raise FileNotFoundError(pdfA)
    if not pdfB.exists(): raise FileNotFoundError(pdfB)
    
    rA = PdfReader(str(pdfA))
    rB = PdfReader(str(pdfB))

    plan = _expand_pattern(pattern, pagesA=len(rA.pages), pagesB=len(rB.pages))

    w = PdfWriter()
    for src, zero_idx in plan:
        if src == "A":
            if zero_idx < 0 or zero_idx >= len(rA.pages):
                raise IndexError(f"A page out of range (0-based): {zero_idx}")
            w.add_page(rA.pages[zero_idx])
        else:
            if zero_idx < 0 or zero_idx >= len(rB.pages):
                raise IndexError(f"B page out of range (0-based): {zero_idx}")
            w.add_page(rB.pages[zero_idx])

    lv_timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    lv_filename = output_path.replace('<yyyymmddhhmmss>.pdf', f'{lv_timestamp}.pdf')
    lv_filepath = Path(lv_filename)
    
    lv_filepath.parent.mkdir(parents=True, exist_ok=True)
    
    with open(lv_filepath, "wb") as f:
        w.write(f)
    print(f"Mixed PDF saved to: {lv_filepath}")
    return lv_filepath

#----------------------------------------------------------------------------
def cli_combinepdfs():
    p = argparse.ArgumentParser(
        description="Mix two PDFs using a single pattern (A/B with page numbers, ranges)."
    )
    p.add_argument("--pdfA", type=str, required=True, default='data/pdf_sample/doc01.pdf', help="First PDF")
    p.add_argument("--pdfB", type=str, default='data/pdf_sample/doc02.pdf', help="Second PDF (optional)")
    p.add_argument("--pattern", type=str, default='(A1-3,B10)x2, Aend-1:2, A5, B1', help="Mix Pattern")
    p.add_argument("--output", type=str, default='tmp/combined.pdf', help="Output PDF file")
    args,_ = p.parse_known_args()

    mix_two_pdfs_single_pattern(
        pdfA=Path(args.pdfA),
        pdfB=Path(args.pdfB),
        pattern=args.pattern,
        output_path=args.output,
    )

#----------------------------------------------------------------------------
if __name__ == "__main__":
    
    print(f'=='*20)
    print(f'Process Name: combinepdfs')
    print(f'=='*20)
    
    cli_combinepdfs()
  
    

#----------------------------------------------------------------------------
#----------------------------------------------------------------------------