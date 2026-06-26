#!/usr/bin/env python3
"""PDF Accessibility Checker — Flask Backend (with error locations)"""
import os
from flask import Flask, request, jsonify, send_from_directory, send_file
import io, os, re
from report_generator import generate_pdf_report
import pikepdf
from pikepdf import Pdf, Name, Array, Dictionary
import pypdf
import pdfplumber

app = Flask(__name__, static_folder="../frontend/build", static_url_path="/")

# ── Helpers ───────────────────────────────────────────────────────────────────

def safe_str(obj):
    try:
        return str(obj)
    except:
        return ""


def get_page_number_from_ref(pdf_pik, page_ref):
    """Return 1-based page number for a pikepdf page object reference."""
    try:
        for idx, page in enumerate(pdf_pik.pages):
            if page.objgen == page_ref.objgen:
                return idx + 1
    except:
        pass
    return None


def traverse_struct_tree(node, pdf_pik=None, tag_counts=None, figures=None,
                         headings=None, tables=None, lists=None, mcid_to_nodes=None,
                         all_headings=None, depth=0, parent_page_num=None):
    if tag_counts is None: tag_counts = {}
    if figures    is None: figures    = []
    if headings   is None: headings   = {}
    if tables     is None: tables     = []
    if lists      is None: lists      = []
    if mcid_to_nodes is None: mcid_to_nodes = {}
    if all_headings is None: all_headings = []

    role_map = {}
    if isinstance(node, Dictionary) and "/RoleMap" in node:
        try:
            for k, v in node["/RoleMap"].items():
                role_map[str(k)] = str(v)
        except:
            pass

    def resolve_tag(t):
        visited = set()
        curr = t
        while curr in role_map:
            if curr in visited:
                break
            visited.add(curr)
            curr = role_map[curr]
        return curr

    def walk(n, parent_page_num, parent_node_info):
        if isinstance(n, Array):
            for item in n:
                walk(item, parent_page_num, parent_node_info)
            return

        if isinstance(n, (int, pikepdf.Integer)):
            mcid = int(n)
            if parent_node_info is not None:
                parent_node_info["mcids"].append(mcid)
                key = (parent_page_num, mcid)
                if key not in mcid_to_nodes:
                    mcid_to_nodes[key] = []
                # Check to avoid duplicates
                if parent_node_info not in mcid_to_nodes[key]:
                    mcid_to_nodes[key].append(parent_node_info)
            return

        if not isinstance(n, Dictionary):
            return

        if n.get("/Type") == "/OBJR":
            return

        tag = safe_str(n.get("/S", ""))
        if not tag:
            if "/K" in n:
                walk(n["/K"], parent_page_num, parent_node_info)
            return

        resolved_tag = resolve_tag(tag)
        tag_counts[resolved_tag] = tag_counts.get(resolved_tag, 0) + 1

        page_num = parent_page_num
        try:
            if "/Pg" in n and pdf_pik is not None:
                resolved_pg = get_page_number_from_ref(pdf_pik, n["/Pg"])
                if resolved_pg is not None:
                    page_num = resolved_pg
        except:
            pass

        node_info = {
            "tag": resolved_tag,
            "tag_raw": tag,
            "page": page_num,
            "mcids": [],
            "node_ref": n
        }

        if resolved_tag == "/Figure":
            page_fig_idx = sum(1 for f in figures if f.get("page") == page_num)
            figures.append({
                "alt": safe_str(n.get("/Alt", "")),
                "page": page_num,
                "page_fig_idx": page_fig_idx,
                "node": n
            })
        elif resolved_tag in ("/H1", "/H2", "/H3", "/H4", "/H5", "/H6", "/H"):
            level = resolved_tag.strip("/")
            if level not in headings:
                headings[level] = []
            headings[level].append({"page": page_num, "node": n})
            all_headings.append({"tag": resolved_tag, "page": page_num, "node": n})
        elif resolved_tag == "/Table":
            tables.append({"node": n, "page": page_num})
        elif resolved_tag == "/L":
            lists.append({"page": page_num, "node": n})

        if "/K" in n:
            walk(n["/K"], page_num, node_info)

    if isinstance(node, Dictionary):
        if "/K" in node:
            walk(node["/K"], parent_page_num, None)

    return tag_counts, figures, headings, tables, lists, mcid_to_nodes, all_headings



def check_table_headers(tables):
    """Returns list of (table_index, has_th, page_num) tuples."""
    results = []
    for i, t in enumerate(tables):
        node = t.get("node", {})
        page_num = t.get("page")
        found_th = False
        kids = node.get("/K", [])
        if isinstance(kids, Array):
            for row in kids:
                if isinstance(row, Dictionary):
                    row_kids = row.get("/K", [])
                    if isinstance(row_kids, Array):
                        for cell in row_kids:
                            if isinstance(cell, Dictionary):
                                if safe_str(cell.get("/S", "")) == "/TH":
                                    found_th = True
        results.append({"index": i + 1, "has_th": found_th, "page": page_num})
    return results


def fmt_pages(pages):
    """Format a list of page numbers nicely, e.g. 'Pages 1, 3, 5'."""
    pages = sorted({p for p in pages if p is not None})
    if not pages:
        return None
    if len(pages) == 1:
        return f"Page {pages[0]}"
    return "Pages " + ", ".join(str(p) for p in pages[:8]) + \
           (" …" if len(pages) > 8 else "")


def extract_visual_elements(pdf_bytes):
    page_lines = {}
    page_tables = {}
    page_images = {}
    page_heights = {}
    visible_tables_detected = False
    visible_lists_detected = False
    list_pattern = re.compile(r'^\s*(?:[•▪\-\*\u2022\u2023\u2043\u254f\u25b8\u25e6]|\d+[\.\)]|[a-zA-Z][\.\)])\s+\w')

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf_plumb:
            for idx, page in enumerate(pdf_plumb.pages):
                page_num = idx + 1
                page_heights[page_num] = float(page.height)
                
                # 1. Tables
                try:
                    tables = page.find_tables()
                    if tables:
                        visible_tables_detected = True
                        page_tables[page_num] = tables
                except:
                    pass
                
                # 2. Images
                try:
                    imgs = page.images
                    if imgs:
                        page_images[page_num] = imgs
                except:
                    pass
                
                # 3. Lines of words
                lines_in_page = []
                try:
                    words = page.extract_words(extra_attrs=["mcid", "fontname", "size"])
                    if words:
                        # Group by top coordinate
                        sorted_words = sorted(words, key=lambda w: (w["top"], w["x0"]))
                        grouped_lines = []
                        current_line = []
                        current_top = None
                        
                        for w in sorted_words:
                            if current_top is None:
                                current_top = w["top"]
                                current_line.append(w)
                            elif abs(w["top"] - current_top) < 3.0:
                                current_line.append(w)
                            else:
                                grouped_lines.append(current_line)
                                current_line = [w]
                                current_top = w["top"]
                        if current_line:
                            grouped_lines.append(current_line)
                        
                        for line_idx, line_words in enumerate(grouped_lines, 1):
                            line_words = sorted(line_words, key=lambda w: w["x0"])
                            text = " ".join(w["text"] for w in line_words).strip()
                            if not text:
                                continue
                            
                            mcids = {w["mcid"] for w in line_words if w.get("mcid") is not None}
                            
                            if list_pattern.match(text):
                                visible_lists_detected = True
                                
                            lines_in_page.append({
                                "line_num": line_idx,
                                "text": text,
                                "mcids": mcids,
                                "top": line_words[0]["top"],
                                "x0": line_words[0]["x0"],
                                "words": line_words
                            })
                except:
                    pass
                page_lines[page_num] = lines_in_page
    except Exception as e:
        print(f"Warning: pdfplumber extraction failed: {e}")

    return page_lines, page_tables, page_images, page_heights, visible_tables_detected, visible_lists_detected



def collect_mcids_from_node(node, pdf_pik=None):
    mcids = []
    pages = []
    
    def walk(n):
        if not isinstance(n, Dictionary):
            return
        
        current_page = None
        try:
            if "/Pg" in n and pdf_pik is not None:
                current_page = get_page_number_from_ref(pdf_pik, n["/Pg"])
        except:
            pass
            
        if "/MCID" in n:
            mcids.append(int(n["/MCID"]))
            if current_page:
                pages.append(current_page)
            return
            
        kids = n.get("/K", None)
        if kids is not None:
            if isinstance(kids, Array):
                for k in kids:
                    if isinstance(k, Dictionary):
                        walk(k)
                    elif isinstance(k, (int, pikepdf.Integer)):
                        mcids.append(int(k))
                        if current_page:
                            pages.append(current_page)
            elif isinstance(kids, Dictionary):
                walk(kids)
            elif isinstance(kids, (int, pikepdf.Integer)):
                mcids.append(int(kids))
                if current_page:
                    pages.append(current_page)
                    
    walk(node)
    return mcids, pages


def get_node_text(node, page_lines, page_num, pdf_pik=None):
    mcids, pages = collect_mcids_from_node(node, pdf_pik)
    
    mcids_by_page = {}
    if mcids:
        for idx, m in enumerate(mcids):
            p = pages[idx] if idx < len(pages) else page_num
            if p:
                mcids_by_page.setdefault(p, set()).add(m)
    
    if not mcids_by_page and page_num:
        mcids_by_page[page_num] = set()
        
    texts = []
    for p, p_mcids in mcids_by_page.items():
        if p in page_lines:
            for line in page_lines[p]:
                if p_mcids and any(m in line["mcids"] for m in p_mcids):
                    texts.append(line["text"])
                    
    if not texts and "/Alt" in node:
        return safe_str(node["/Alt"])
    return " ".join(texts).strip()


def get_closest_line_for_rect(page_num, rect, page_lines, page_height):
    if not rect or len(rect) < 4 or page_num not in page_lines or not page_lines[page_num]:
        return 1, ""
    try:
        y_center = (float(rect[1]) + float(rect[3])) / 2
        annot_top = page_height - y_center
        lines = page_lines[page_num]
        closest_line = min(lines, key=lambda l: abs(l["top"] - annot_top))
        return closest_line["line_num"], closest_line["text"]
    except:
        return 1, ""


def get_node_line_number(node, page_lines, page_num, pdf_pik=None):
    mcids, pages = collect_mcids_from_node(node, pdf_pik)
    target_page = page_num
    if pages:
        target_page = pages[0]
    if not target_page or target_page not in page_lines or not page_lines[target_page]:
        return 1
    lines = page_lines[target_page]
    for line in lines:
        if mcids and any(m in line["mcids"] for m in mcids):
            return line["line_num"]
    return 1


def get_surrounding_text_for_node(node, page_lines, page_images, page_num, pdf_pik=None, page_fig_idx=0):
    mcids, pages = collect_mcids_from_node(node, pdf_pik)
    
    target_page = page_num
    if pages:
        target_page = pages[0]
        
    if not target_page:
        return {"page": 1, "line_num": 1, "context": "", "text": "Unknown page"}
        
    if target_page not in page_lines or not page_lines[target_page]:
        return {"page": target_page, "line_num": 1, "context": "", "text": "Empty page"}
        
    lines = page_lines[target_page]
    
    # Try using sorted page images
    sorted_imgs = []
    if target_page in page_images:
        try:
            sorted_imgs = sorted(page_images[target_page], key=lambda x: (x.get("top", 0), x.get("x0", 0)))
        except:
            sorted_imgs = page_images[target_page]
            
    img_top = None
    if sorted_imgs and page_fig_idx < len(sorted_imgs):
        img_top = sorted_imgs[page_fig_idx].get("top")
        
    if img_top is not None and lines:
        closest_line = min(lines, key=lambda l: abs(l["top"] - img_top))
        try:
            idx = lines.index(closest_line)
            prev_text = lines[idx-1]["text"][:25] + "..." if idx > 0 else ""
            next_text = lines[idx+1]["text"][:25] + "..." if idx < len(lines) - 1 else ""
            context = []
            if prev_text: context.append(f"after '{prev_text}'")
            if next_text: context.append(f"before '{next_text}'")
            ctx_str = f" ({', '.join(context)})" if context else ""
            return {
                "page": target_page,
                "line_num": closest_line["line_num"],
                "context": ctx_str,
                "text": closest_line["text"]
            }
        except:
            return {
                "page": target_page,
                "line_num": closest_line["line_num"],
                "context": "",
                "text": closest_line["text"]
            }
            
    # Fallback to MCIDs matching
    for idx, line in enumerate(lines):
        if mcids and any(m in line["mcids"] for m in mcids):
            prev_text = lines[idx-1]["text"][:25] + "..." if idx > 0 else ""
            next_text = lines[idx+1]["text"][:25] + "..." if idx < len(lines) - 1 else ""
            context = []
            if prev_text: context.append(f"after '{prev_text}'")
            if next_text: context.append(f"before '{next_text}'")
            ctx_str = f" ({', '.join(context)})" if context else ""
            return {
                "page": target_page,
                "line_num": line["line_num"],
                "context": ctx_str,
                "text": line["text"]
            }
            
    # Fallback to first line of page
    first_line = lines[0]
    return {
        "page": target_page,
        "line_num": first_line["line_num"],
        "context": " (top of page)",
        "text": first_line["text"]
    }



def find_untagged_headings(page_lines, mcid_to_nodes):
    untagged_headings = []
    for page_num, lines in page_lines.items():
        if not lines:
            continue
        sizes = []
        for line in lines:
            for w in line.get("words", []):
                if isinstance(w.get("size"), (int, float)):
                    sizes.append(w["size"])
        if not sizes:
            continue
        import statistics
        try:
            body_size = statistics.median(sizes)
        except:
            body_size = 10.0
            
        for line in lines:
            text = line["text"]
            if len(text) < 3 or len(text) > 80:
                continue
                
            line_words = line.get("words", [])
            if not line_words:
                continue
            avg_size = sum(w.get("size", 10) for w in line_words) / len(line_words)
            is_bold = any("bold" in str(w.get("fontname", "")).lower() for w in line_words)
            
            line_tags = set()
            for mcid in line["mcids"]:
                nodes = mcid_to_nodes.get((page_num, mcid), [])
                for n in nodes:
                    if n.get("tag"):
                        line_tags.add(n["tag"])
            
            has_heading_tag = any(t in ("/H1", "/H2", "/H3", "/H4", "/H5", "/H6", "/H") for t in line_tags)
            
            if not has_heading_tag:
                if avg_size > body_size * 1.2 or (is_bold and avg_size >= body_size * 1.0):
                    if not any(t in ("/L", "/LI", "/Table", "/TR", "/TH", "/TD") for t in line_tags):
                        untagged_headings.append({
                            "page": page_num,
                            "line_num": line["line_num"],
                            "text": text
                        })
    return untagged_headings



# ── Main checker ──────────────────────────────────────────────────────────────

def run_checks(pdf_bytes: bytes) -> list:
    results = []

    try:
        pdf_pik = Pdf.open(io.BytesIO(pdf_bytes))
    except Exception as e:
        return [{"id": i + 1, "name": f"Check {i + 1}", "status": "FAIL",
                 "detail": f"Cannot open PDF: {e}", "locations": []} for i in range(10)]

    try:
        pdf_pyp = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    except:
        pdf_pyp = None

    root     = pdf_pik.Root
    num_pages = len(pdf_pik.pages)

    # ── Visual Layout Parsing via pdfplumber ──────────────────────────────
    page_lines, page_tables, page_images, page_heights, visible_tables_detected, visible_lists_detected = extract_visual_elements(pdf_bytes)

    # Structure tree
    tag_counts, figures, headings, tables, lists = {}, [], {}, [], []
    mcid_to_nodes = {}
    all_headings = []
    has_struct = "/StructTreeRoot" in root
    has_mark   = "/MarkInfo" in root
    marked     = False

    if has_mark:
        try:
            marked = bool(root["/MarkInfo"].get("/Marked", False))
        except:
            pass

    if has_struct:
        try:
            tag_counts, figures, headings, tables, lists, mcid_to_nodes, all_headings = traverse_struct_tree(
                root["/StructTreeRoot"], pdf_pik, mcid_to_nodes=mcid_to_nodes)
        except:
            pass

    # Flatten heading counts for backward compat
    heading_counts = {k: len(v) for k, v in headings.items()}

    # ── 1. Article Reading Order ───────────────────────────────────────────
    # Check if there are legacy PDF article threads OR structural /Art tags
    has_legacy_articles = "/Threads" in root or "/Articles" in root
    has_structural_articles = "/Art" in tag_counts or "/Article" in tag_counts
    
    if has_legacy_articles or has_structural_articles:
        results.append({
            "id": 1, "name": "Article Reading Order (Need to enable USE for Reading Order in Article panel)", "status": "PASS",
            "detail": "Reading order structure validated (Logical Article tags or Threads found).",
            "locations": []
        })
    else:
        locs = []
        count = 0
        for p in range(1, num_pages + 1):
            if p in page_lines:
                for line in page_lines[p]:
                    locs.append({
                        "label": f"Page {p}, Line {line['line_num']}",
                        "description": f"No article reading thread defined for line: \"{line['text'][:40]}...\""
                    })
                    count += 1
                    if count >= 5:
                        break
            if count >= 5:
                break
        if not locs:
            first_line_text = ""
            if 1 in page_lines and page_lines[1]:
                first_line_text = f" starting near: \"{page_lines[1][0]['text'][:40]}...\""
            locs.append({
                "label": "Page 1, Line 1",
                "description": f"No article threads detected{first_line_text}. Article threads must be set in the source file (InDesign), not fixable page-by-page in Acrobat."
            })
            
        results.append({
            "id": 1, "name": "Article Reading Order (Need to enable USE for Reading Order in Article panel)", "status": "FAIL",
            "detail": "No Article threads detected. Enable 'USE for Reading Order' in the InDesign Article panel before re-exporting.",
            "locations": locs
        })

    # ── 2. Tagging Structure ───────────────────────────────────────────────
    has_h     = any(k in tag_counts for k in ["/H1","/H2","/H3","/H4","/H5","/H6","/H"])
    has_l     = "/L" in tag_counts
    has_table = "/Table" in tag_counts

    untagged_headings = find_untagged_headings(page_lines, mcid_to_nodes)
    
    untagged_list_items = []
    list_pattern = re.compile(r'^\s*(?:[•▪\-\*\u2022\u2023\u2043\u254f\u25b8\u25e6]|\d+[\.\)]|[a-zA-Z][\.\)])\s+\w')
    for page_num, lines in page_lines.items():
        for line in lines:
            if list_pattern.match(line["text"]):
                line_tags = set()
                for mcid in line["mcids"]:
                    nodes = mcid_to_nodes.get((page_num, mcid), [])
                    for n in nodes:
                        if n.get("tag"):
                            line_tags.add(n["tag"])
                has_list_tag = any(t in ("/L", "/LI", "/Lbl", "/LBody") for t in line_tags)
                if not has_list_tag:
                    untagged_list_items.append({
                        "page": page_num,
                        "line_num": line["line_num"],
                        "text": line["text"]
                    })

    untagged_tables = []
    for page_num, v_tables in page_tables.items():
        has_table_tag_on_page = any(t.get("page") == page_num for t in tables)
        if not has_table_tag_on_page:
            for v_idx, vt in enumerate(v_tables, 1):
                line_preview = ""
                line_num = 1
                try:
                    bbox = vt.bbox
                    top, bottom = bbox[1], bbox[3]
                    tbl_lines = [line for line in page_lines.get(page_num, []) if top <= line["top"] <= bottom]
                    if tbl_lines:
                        line_preview = f" (near \"{tbl_lines[0]['text'][:30]}...\")"
                        line_num = tbl_lines[0]["line_num"]
                except:
                    pass
                untagged_tables.append({
                    "page": page_num,
                    "line_num": line_num,
                    "preview": f"Visual Table #{v_idx}{line_preview}"
                })

    # Check skipped heading levels
    skipped_headings = []
    for idx in range(len(all_headings) - 1):
        h1_item = all_headings[idx]
        h2_item = all_headings[idx + 1]
        try:
            l1 = int(h1_item["tag"].strip("/H"))
            l2 = int(h2_item["tag"].strip("/H"))
            if l2 > l1 + 1:
                skipped_headings.append((h1_item, h2_item))
        except:
            pass

    if has_mark and marked and has_struct:
        locs = []
        if skipped_headings:
            for h1_item, h2_item in skipped_headings[:5]:
                h2_text = get_node_text(h2_item["node"], page_lines, h2_item["page"], pdf_pik)
                h2_preview = f"\"{h2_text[:40]}...\"" if len(h2_text) > 40 else (f"\"{h2_text}\"" if h2_text else "Unnamed Heading")
                line_num = get_node_line_number(h2_item["node"], page_lines, h2_item["page"], pdf_pik)
                locs.append({
                    "label": f"Page {h2_item['page']}, Line {line_num}",
                    "description": f"Warning: Heading level skips illogically: {h1_item['tag']} -> {h2_item['tag']} for heading {h2_preview}."
                })
        for uh in untagged_headings[:5]:
            locs.append({
                "label": f"Page {uh['page']}, Line {uh['line_num']}",
                "description": f"Warning: Visual heading text looks untagged: \"{uh['text']}\"."
            })
        for ul in untagged_list_items[:5]:
            locs.append({
                "label": f"Page {ul['page']}, Line {ul['line_num']}",
                "description": f"Warning: Visual list item is untagged: \"{ul['text']}\"."
            })
        for ut in untagged_tables[:5]:
            locs.append({
                "label": f"Page {ut['page']}, Line {ut['line_num']}",
                "description": f"Warning: Visual table is untagged: {ut['preview']}."
            })
            
        detail_msg = f"Tagged PDF — Headings ({len(all_headings)}) ✓  Lists ({tag_counts.get('/L',0)}) ✓  Tables ({tag_counts.get('/Table',0)}) ✓"
        if locs:
            detail_msg += f" (with {len(locs)} structural warnings)"

        results.append({
            "id": 2, "name": "Tagging Structure for Headings, List and Tables", "status": "FAIL" if locs else "PASS",
            "detail": detail_msg,
            "locations": locs
        })
    else:
        missing = []
        locs = []
        if not (has_mark and marked and has_struct):
            missing.append("document not tagged/marked")
            first_line_text = ""
            if 1 in page_lines and page_lines[1]:
                first_line_text = f" starting near: \"{page_lines[1][0]['text'][:40]}...\""
            locs.append({
                "label": "Page 1, Line 1",
                "description": f"No MarkInfo or StructTreeRoot in PDF root{first_line_text} — document is completely untagged."
            })
        detail_msg = "Issues: " + "; ".join(missing) + "." if missing else "Issues: untagged/unmarked document."
        results.append({
            "id": 2, "name": "Tagging Structure for Headings, List and Tables", "status": "FAIL",
            "detail": detail_msg,
            "locations": locs
        })

    # ── 3. Table Heading (TH / Convert to Header Rows) ────────────────────
    if not tables:
        results.append({
            "id": 3, "name": "Convert to Header Rows", "status": "PASS",
            "detail": "No tables found in document.",
            "locations": []
        })
    else:
        bad_tables = []
        for i, t in enumerate(tables, 1):
            node = t.get("node", {})
            page_num = t.get("page")
            found_th = False
            
            def check_th(n):
                if not isinstance(n, Dictionary):
                    return False
                if safe_str(n.get("/S", "")) == "/TH":
                    return True
                kids = n.get("/K", None)
                if kids is not None:
                    if isinstance(kids, Array):
                        for k in kids:
                            if check_th(k):
                                return True
                    elif isinstance(kids, Dictionary):
                        return check_th(kids)
                return False
                
            found_th = check_th(node)
            
            if not found_th:
                preview = get_node_text(node, page_lines, page_num, pdf_pik)
                preview_clean = preview[:50] + "..." if len(preview) > 50 else (preview or "Empty Table")
                line_num = get_node_line_number(node, page_lines, page_num, pdf_pik)
                bad_tables.append({
                    "index": i,
                    "page": page_num,
                    "line_num": line_num,
                    "preview": preview_clean
                })
                
        if not bad_tables:
            results.append({
                "id": 3, "name": "Convert to Header Rows", "status": "PASS",
                "detail": f"All {len(tables)} table(s) have header rows (TH tags).",
                "locations": []
            })
        else:
            locs = []
            for bt in bad_tables:
                locs.append({
                    "label": f"Page {bt['page']}, Line {bt['line_num']}",
                    "description": f"Table starting with \"{bt['preview']}\" is missing header rows. No TH (table header) cells found in the structure tree. Ensure the table's first row is marked with TH tags."
                })
            results.append({
                "id": 3, "name": "Convert to Header Rows", "status": "FAIL",
                "detail": f"{len(bad_tables)} table(s) missing TH cells.",
                "locations": locs
            })

    # ── 4. Annotations & Form Fields Reading Order ─────────────────────────
    annot_issues = []
    annot_locs   = []
    for page_num, page in enumerate(pdf_pik.pages, 1):
        annots = page.get("/Annots", [])
        if isinstance(annots, Array) and annots:
            tabs = safe_str(page.get("/Tabs", ""))
            p_height = page_heights.get(page_num, 792)
            
            names = []
            for a in annots:
                try:
                    resolved = pdf_pik.get_object(a.objgen) if hasattr(a, "objgen") else a
                    if isinstance(resolved, Dictionary):
                        name = ""
                        if "/T" in resolved:
                            name = safe_str(resolved["/T"])
                        elif "/Contents" in resolved:
                            name = safe_str(resolved["/Contents"])
                        if not name:
                            sub = safe_str(resolved.get("/Subtype", "")).strip("/")
                            name = f"Unnamed {sub or 'Annotation'}"
                        names.append(name)
                        
                        rect = resolved.get("/Rect")
                        line_num, line_text = get_closest_line_for_rect(page_num, rect, page_lines, p_height)
                        
                        desc_text = f" (near line: \"{line_text[:30]}...\")" if line_text else ""
                        
                        if tabs and tabs != "/S":
                            annot_locs.append({
                                "label": f"Page {page_num}, Line {line_num}",
                                "description": f"Annotation '{name}'{desc_text} — Tab order is '{tabs}' (must be '/S'). Fix in Acrobat: Page Properties → Tab Order → Use Document Structure."
                            })
                        elif not tabs:
                            annot_locs.append({
                                "label": f"Page {page_num}, Line {line_num}",
                                "description": f"Annotation '{name}'{desc_text} — Tab order is not set (must be '/S'). Right-click page thumbnail → Page Properties → Tab Order → Use Document Structure."
                            })
                except:
                    pass
            
            if names:
                if tabs and tabs != "/S":
                    annot_issues.append(f"Page {page_num}: Tab order is '{tabs}'")
                elif not tabs:
                    annot_issues.append(f"Page {page_num}: Tab order not set")

    if not annot_issues:
        results.append({
            "id": 4, "name": "Annotations & Form Fields Reading Order", "status": "PASS",
            "detail": "All annotations/form fields have correct reading order.",
            "locations": []
        })
    else:
        results.append({
            "id": 4, "name": "Annotations & Form Fields Reading Order", "status": "FAIL",
            "detail": "; ".join(annot_issues[:5]) + ". Configure page Tab Order to Use Document Structure.",
            "locations": annot_locs[:10]
        })

    # ── 5. Alt-text on Images ──────────────────────────────────────────────
    no_alt      = [f for f in figures if not f.get("alt","").strip()]
    artifact_ok = [f for f in figures if f.get("alt","").strip().lower() == "artifact"]

    if not figures:
        results.append({
            "id": 5, "name": "Alt-text on Images", "status": "PASS",
            "detail": "No figure elements found in tag structure.",
            "locations": []
        })
    else:
        locs = []
        for fig in no_alt[:10]:
            info = get_surrounding_text_for_node(fig["node"], page_lines, page_images, fig["page"], pdf_pik, fig.get("page_fig_idx", 0))
            locs.append({
                "label": f"Page {info['page']}, Line {info['line_num']}",
                "description": f"Warning: Figure missing alt-text{info['context']} near text: \"{info['text'][:40]}...\". In Acrobat: right-click image → Edit Alternate Text."
            })
        
        detail_msg = f"{len(figures)} image(s) analyzed — {len(artifact_ok)} Artifact (decorative), {len(figures) - len(no_alt) - len(artifact_ok)} described"
        if no_alt:
            detail_msg += f" (with {len(no_alt)} warnings for missing alt-text)"
            
        results.append({
            "id": 5, "name": "Alt-text on Images", "status": "FAIL" if no_alt else "PASS",
            "detail": detail_msg,
            "locations": locs
        })

    # ── 6. Images Reading Order + Alt Text (TK placeholder) ───────────────
    tk_images = [f for f in figures if not f.get("alt","").strip() or
                 "TK" in f.get("alt","").strip().upper()]

    if not figures:
        results.append({
            "id": 6, "name": "Images Reading Order with Alt Text", "status": "PASS",
            "detail": "No images in structure tree.",
            "locations": []
        })
    else:
        locs = []
        for fig in tk_images[:10]:
            info = get_surrounding_text_for_node(fig["node"], page_lines, page_images, fig["page"], pdf_pik, fig.get("page_fig_idx", 0))
            alt_val = fig.get("alt", "").strip() or "empty"
            locs.append({
                "label": f"Page {info['page']}, Line {info['line_num']}",
                "description": f"Warning: Image has placeholder/empty alt-text ({alt_val}){info['context']} near text: \"{info['text'][:40]}...\"."
            })
            
        detail_msg = f"All {len(figures)} image(s) in logical reading sequence"
        if tk_images:
            detail_msg += f" (with {len(tk_images)} placeholder warnings)"
            
        results.append({
            "id": 6, "name": "Images Reading Order with Alt Text", "status": "FAIL" if tk_images else "PASS",
            "detail": detail_msg,
            "locations": locs
        })

    # ── 7. Bookmarks for Heading Levels (H1) ──────────────────────────────
    has_outlines  = "/Outlines" in root
    outline_count = 0
    if has_outlines:
        try:
            node  = root["/Outlines"]
            first = node.get("/First", None)
            if first:
                outline_count = 1
                cur = first
                while "/Next" in cur:
                    outline_count += 1
                    cur = cur["/Next"]
        except:
            pass

    h1_count = heading_counts.get("H1", 0)
    h1_list = headings.get("H1", [])

    if has_outlines and outline_count > 0:
        results.append({
            "id": 7, "name": "Bookmarks for Heading Levels (H1)", "status": "PASS",
            "detail": f"{outline_count} bookmark(s) found. H1 tags in structure: {h1_count}.",
            "locations": []
        })
    else:
        locs = []
        if h1_list:
            for entry in h1_list[:10]:
                h1_text = get_node_text(entry["node"], page_lines, entry["page"], pdf_pik)
                h1_preview = f"\"{h1_text[:40]}...\"" if len(h1_text) > 40 else (f"\"{h1_text}\"" if h1_text else "Unnamed Heading")
                line_num = get_node_line_number(entry["node"], page_lines, entry["page"], pdf_pik)
                locs.append({
                    "label": f"Page {entry['page']}, Line {line_num}",
                    "description": f"H1 heading {h1_preview} detected in tag structure but no outline bookmark exists. Enable 'Include Bookmarks' in InDesign export, or add manually in Acrobat Bookmarks panel."
                })
        else:
            first_line_text = ""
            if 1 in page_lines and page_lines[1]:
                first_line_text = f" starting near: \"{page_lines[1][0]['text'][:40]}...\""
            locs.append({
                "label": "Page 1, Line 1",
                "description": f"No H1 tags detected and no bookmarks present{first_line_text}. Enable PDF Bookmarks in InDesign export settings."
            })
        results.append({
            "id": 7, "name": "Bookmarks for Heading Levels (H1)", "status": "FAIL",
            "detail": f"No bookmarks found. Add bookmarks for all H1 headings "
                      f"({h1_count} H1 tag(s) detected). Enable PDF Bookmarks in InDesign export.",
            "locations": locs
        })

    # ── 8. Document Title ──────────────────────────────────────────────────
    title = ""
    try:
        if "/Info" in pdf_pik.trailer:
            title = safe_str(pdf_pik.trailer["/Info"].get("/Title", ""))
    except:
        pass

    display_doc_title = False
    try:
        vp = root.get("/ViewerPreferences", None)
        if vp:
            display_doc_title = safe_str(
                vp.get("/DisplayDocTitle","false")).lower() in ("true","/true")
    except:
        pass

    lang = safe_str(root.get("/Lang", ""))

    issues_8 = []
    locs_8   = []
    
    first_text = ""
    first_page = 1
    first_line = 1
    
    h1_list = headings.get("H1", [])
    if h1_list:
        first_h1 = h1_list[0]
        first_page = first_h1["page"] or 1
        first_text = get_node_text(first_h1["node"], page_lines, first_page, pdf_pik)
        if first_page in page_lines:
            for l in page_lines[first_page]:
                if first_text and first_text in l["text"]:
                    first_line = l["line_num"]
                    break
    else:
        for p in range(1, num_pages + 1):
            if p in page_lines and page_lines[p]:
                first_text = page_lines[p][0]["text"]
                first_page = p
                first_line = page_lines[p][0]["line_num"]
                break
                
    first_text_clean = first_text[:40] + "..." if len(first_text) > 40 else first_text

    title_missing = not title or title.strip() in ('', 'Untitled')
    if title_missing:
        issues_8.append("Title is missing or empty")
        locs_8.append({
            'label': f"Page {first_page}, Line {first_line}",
            'description': f"Document title is missing. The first line of text is \"{first_text_clean}\". Fix: Set Title in Acrobat under File → Properties → Description → Title."
        })
        
    warnings_8 = []
    if not lang:
        warnings_8.append("Document language not set")
        locs_8.append({
            'label': f"Page {first_page}, Line {first_line}",
            'description': f"Warning: Document language is not set. Fix: Set Language (e.g. 'en') under File → Properties → Advanced → Language."
        })
    if not display_doc_title:
        warnings_8.append("'Display Document Title' not enabled")
        locs_8.append({
            'label': f"Page {first_page}, Line {first_line}",
            'description': "Warning: 'Display Document Title' is off (currently showing file name instead of title). Fix: Acrobat → File → Properties → Initial View → Show: Document Title."
        })
        
    if not title_missing:
        detail_msg = f"Title: '{title}'"
        if lang:
            detail_msg += f" | Language: '{lang}'"
        if display_doc_title:
            detail_msg += " | DisplayDocTitle: on"
        if warnings_8:
            detail_msg += f" (with {len(warnings_8)} warnings: {', '.join(warnings_8)})"
            
        results.append({
            "id": 8, "name": "Document Title", "status": "FAIL" if warnings_8 else "PASS",
            "detail": detail_msg,
            "locations": locs_8
        })
    else:
        results.append({
            "id": 8, "name": "Document Title", "status": "FAIL",
            "detail": "Document title metadata is missing or empty. Fix in Acrobat: File → Properties.",
            "locations": locs_8
        })

    # ── 9. Reading Order in Export PDF ────────────────────────────────────
    vp_issues = []
    vp_locs   = []
    
    first_line_text = ""
    if 1 in page_lines and page_lines[1]:
        first_line_text = f" starting near: \"{page_lines[1][0]['text'][:40]}...\""
        
    try:
        vp = root.get("/ViewerPreferences", None)
        if vp is None:
            vp_issues.append("ViewerPreferences not set")
            vp_locs.append({
                "label": "Page 1, Line 1",
                "description": f"No ViewerPreferences dictionary in PDF root{first_line_text}. Add via Acrobat: File → Properties → Initial View."
            })
    except:
        vp_issues.append("Cannot read ViewerPreferences")
        vp_locs.append({
            "label": "Page 1, Line 1",
            "description": f"ViewerPreferences could not be read{first_line_text}."
        })

    if not (has_mark and has_struct):
        vp_issues.append("Document not tagged — reading order in Export PDF incorrect")
        vp_locs.append({
            "label": "Page 1, Line 1",
            "description": f"Document lacks tagging structure (MarkInfo / StructTreeRoot){first_line_text}. Re-export from InDesign with accessibility tagging, or run Acrobat → Accessibility → Add Tags to Document."
        })
    else:
        # Check for figure reading order mismatch
        reading_order_mismatches = []
        for page_num in range(1, num_pages + 1):
            page_figs = [f for f in figures if f.get("page") == page_num]
            if len(page_figs) > 1:
                fig_coords = []
                for idx, fig in enumerate(page_figs):
                    node = fig["node"]
                    fig_mcids = []
                    def get_mcids(n):
                        if not isinstance(n, Dictionary):
                            return
                        if "/MCID" in n:
                            fig_mcids.append(int(n["/MCID"]))
                            return
                        kids = n.get("/K", None)
                        if kids is not None:
                            if isinstance(kids, Array):
                                for k in kids:
                                    if isinstance(k, (int, pikepdf.Integer)):
                                        fig_mcids.append(int(k))
                                    elif isinstance(k, Dictionary):
                                        get_mcids(k)
                            elif isinstance(kids, (int, pikepdf.Integer)):
                                fig_mcids.append(int(kids))
                            elif isinstance(kids, Dictionary):
                                get_mcids(kids)
                    get_mcids(node)
                    
                    top = None
                    if page_num in page_lines:
                        for line in page_lines[page_num]:
                            if any(m in line["mcids"] for m in fig_mcids):
                                top = line["top"]
                                break
                    if top is not None:
                        fig_coords.append((fig, top))
                
                if len(fig_coords) > 1:
                    sorted_by_top = sorted(fig_coords, key=lambda x: x[1])
                    original_seq = [x[0] for x in fig_coords]
                    sorted_seq = [x[0] for x in sorted_by_top]
                    if original_seq != sorted_seq:
                        reading_order_mismatches.append((page_num, page_figs[0]))
                        
        for p, first_fig in reading_order_mismatches[:5]:
            vp_issues.append(f"Page {p}: Figure reading order mismatch")
            line_num = get_node_line_number(first_fig["node"], page_lines, p, pdf_pik)
            info = get_surrounding_text_for_node(first_fig["node"], page_lines, page_images, p, pdf_pik, first_fig.get("page_fig_idx", 0))
            vp_locs.append({
                "label": f"Page {p}, Line {line_num}",
                "description": f"Figures are tagged in a different sequence in the structure tree than they appear visually on the page near text: \"{info['text'][:35]}...\". Fix reading order in Acrobat using the Reading Order panel."
            })

    if not vp_issues:
        results.append({
            "id": 9, "name": "Reading Order in Export PDF", "status": "PASS",
            "detail": "Document tagged with structure. Reading order preserved in export.",
            "locations": []
        })
    else:
        results.append({
            "id": 9, "name": "Reading Order in Export PDF", "status": "FAIL",
            "detail": "; ".join(vp_issues) + ". Verify 'Reading Order' in Acrobat Accessibility settings.",
            "locations": vp_locs
        })

    # ── 10. Accessibility Check (overall) ─────────────────────────────────
    acc_issues = []
    acc_locs   = []

    if not (has_mark and marked) or not has_struct:
        missing_parts = []
        desc_parts = []
        if not (has_mark and marked):
            missing_parts.append('Not tagged/marked')
            desc_parts.append('MarkInfo.Marked is false — re-export from InDesign with Create Tagged PDF enabled.')
        if not has_struct:
            missing_parts.append('No StructTreeRoot')
            desc_parts.append('No StructTreeRoot — use Acrobat Accessibility → Add Tags to Document.')
        acc_issues.append('; '.join(missing_parts))
        acc_locs.append({
            'label': 'Page 1, Line 1',
            'description': ' '.join(desc_parts) + f" (first line text: \"{first_text_clean}\")"
        })
    if not lang:
        acc_locs.append({
            "label": f"Page {first_page}, Line {first_line}",
            "description": "Warning: No /Lang entry on the document root. Set via Acrobat → File → Properties → Advanced → Language."
        })
    if not title or title.strip() in ("", "Untitled"):
        acc_issues.append("Document title missing")
        acc_locs.append({
            "label": f"Page {first_page}, Line {first_line}",
            "description": f"Title field is empty. Set via Acrobat → File → Properties → Description → Title. First line text: \"{first_text_clean}\""
        })
    if has_struct and not ("/P" in tag_counts):
        acc_issues.append("No paragraph (P) tags")
        p_locs = []
        count = 0
        for p in range(1, num_pages + 1):
            if p in page_lines:
                for line in page_lines[p]:
                    p_locs.append({
                        "label": f"Page {p}, Line {line['line_num']}",
                        "description": f"Untagged body text: \"{line['text'][:40]}...\" (missing Paragraph /P tag)."
                    })
                    count += 1
                    if count >= 5:
                        break
            if count >= 5:
                break
        if p_locs:
            acc_locs.extend(p_locs)
        else:
            acc_locs.append({
                "label": "Page 1, Line 1",
                "description": f"No /P (paragraph) tags in the structure tree. Body text is not tagged. Re-tag from InDesign or use Acrobat's Reading Order tool. First line text: \"{first_text_clean}\""
            })
    if no_alt:
        for fig in no_alt[:5]:
            info = get_surrounding_text_for_node(fig["node"], page_lines, page_images, fig["page"], pdf_pik, fig.get("page_fig_idx", 0))
            acc_locs.append({
                "label": f"Page {info['page']}, Line {info['line_num']}",
                "description": f"Warning: Image is missing alt-text{info['context']} near text: \"{info['text'][:40]}...\". Right-click figure in Acrobat -> Properties -> Alternate Text."
            })

    p_count   = tag_counts.get("/P", 0)
    h_count   = sum(tag_counts.get(h, 0) for h in ["/H1","/H2","/H3","/H4","/H5","/H6","/H"])
    tbl_count = tag_counts.get("/Table", 0)
    fig_count = len(figures)

    if not acc_issues:
        detail_msg = f"All checks passed. Tags — P:{p_count}, Headings:{h_count}, Tables:{tbl_count}, Figures:{fig_count}."
        if acc_locs:
            detail_msg += f" (with {len(acc_locs)} warnings)"
        results.append({
            "id": 10, "name": "Accessibility Check (Para, Headings, Tables, Figures)", "status": "FAIL" if acc_locs else "PASS",
            "detail": detail_msg,
            "locations": acc_locs
        })
    else:
        results.append({
            "id": 10, "name": "Accessibility Check (Para, Headings, Tables, Figures)", "status": "FAIL",
            "detail": "; ".join(acc_issues) + ". Run 'Full Check' in Acrobat Accessibility tool.",
            "locations": acc_locs
        })

    return results


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return response

@app.route("/check", methods=["POST", "OPTIONS"])
def check():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported"}), 400
    results = run_checks(f.read())
    return jsonify({"results": results, "filename": f.filename})

@app.route("/report", methods=["POST", "OPTIONS"])
def report():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json()
    if not data or "results" not in data or "filename" not in data:
        return jsonify({"error": "Invalid request data"}), 400
    results = data["results"]
    filename = data["filename"]
    try:
        pdf_bytes = generate_pdf_report(results, filename)
        base_name = os.path.splitext(filename)[0]
        report_name = f"accessibility_report_{base_name}.pdf"
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=report_name
        )
    except Exception as e:
        return jsonify({"error": f"Failed to generate report: {str(e)}"}), 500

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_react(path):
    build_dir = os.path.join(os.path.dirname(__file__), "../frontend/build")
    if path and os.path.exists(os.path.join(build_dir, path)):
        return send_from_directory(build_dir, path)
    return send_from_directory(build_dir, "index.html")
@app.route("/")
def home():
    return "Flask app is running!"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
    
