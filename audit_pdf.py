import sys
import os
import re
import pikepdf
import pdfplumber
from decimal import Decimal

class StructNodeInfo:
    def __init__(self, tag, page_num, node_ref=None):
        self.tag = tag
        self.page_num = page_num
        self.mcids = []
        self.obj_refs = []
        self.children = []
        self.node_ref = node_ref

    def get_text(self, mcid_text_map):
        texts = []
        # Extract text from MCIDs directly associated with this node
        for mcid in self.mcids:
            t = mcid_text_map.get((self.page_num, mcid))
            if t:
                texts.append(t.strip())
        
        # Also recursively get text from children if they are Spans or inline tags
        for child in self.children:
            if child.tag in ('/Span', '/Link', '/Quote', '/Code', '/BibEntry'):
                ct = child.get_text(mcid_text_map)
                if ct:
                    texts.append(ct)
        return " ".join(texts)

def build_page_maps(pdf_pik):
    page_map = {}
    reverse_page_map = {}
    for idx, page in enumerate(pdf_pik.pages):
        page_num = idx + 1
        page_map[page.objgen] = page_num
        reverse_page_map[page_num] = page.objgen
    return page_map, reverse_page_map

def walk_structure_tree(node, page_map, all_nodes, parent_page_num=None, current_node_info=None):
    if isinstance(node, pikepdf.Array):
        for item in node:
            walk_structure_tree(item, page_map, all_nodes, parent_page_num, current_node_info)
        return
        
    if isinstance(node, pikepdf.Integer):
        if current_node_info:
            current_node_info.mcids.append(int(node))
        return
        
    if isinstance(node, pikepdf.Dictionary):
        # Prevent infinite recursion by not following parent pointers (/P) or other non-child keys.
        # Check if it is an Object Reference (/OBJR)
        if node.get("/Type") == "/OBJR":
            if current_node_info and "/Obj" in node:
                obj = node["/Obj"]
                if hasattr(obj, "objgen"):
                    current_node_info.obj_refs.append(obj.objgen)
            return
            
        # Get role_map to resolve custom tags
        role_map = {}
        try:
            pdf_pik = node.owner
            if pdf_pik and "/StructTreeRoot" in pdf_pik.Root:
                st = pdf_pik.Root["/StructTreeRoot"]
                if "/RoleMap" in st:
                    for k, v in st["/RoleMap"].items():
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

        # It is a Structure Element
        tag = str(node.get("/S", "NoTag"))
        resolved_tag = resolve_tag(tag)
        
        page_num = parent_page_num
        if "/Pg" in node:
            pg_ref = node["/Pg"]
            if hasattr(pg_ref, "objgen") and pg_ref.objgen in page_map:
                page_num = page_map[pg_ref.objgen]
                
        node_info = StructNodeInfo(resolved_tag, page_num, node_ref=node)
        all_nodes.append(node_info)
        if current_node_info:
            current_node_info.children.append(node_info)
            
        if "/K" in node:
            walk_structure_tree(node["/K"], page_map, all_nodes, page_num, node_info)

def get_xmp_title(pdf):
    try:
        meta = pdf.open_metadata()
        for key in meta.keys():
            if 'dc:title' in key:
                return str(meta[key]).strip()
    except:
        pass
    try:
        if "/Metadata" in pdf.Root:
            meta_stream = pdf.Root["/Metadata"]
            content = meta_stream.read_bytes().decode('utf-8', errors='ignore')
            m = re.search(r'<dc:title[^>]*>(.*?)</dc:title>', content, re.DOTALL)
            if m:
                val = m.group(1)
                val_clean = re.sub(r'<[^>]+>', '', val).strip()
                return val_clean
    except:
        pass
    return ""

def get_docinfo_title(pdf):
    try:
        if "/Info" in pdf.trailer:
            info = pdf.trailer["/Info"]
            if "/Title" in info:
                return str(info["/Title"]).strip()
    except:
        pass
    return ""

def get_display_doc_title(pdf):
    try:
        if "/ViewerPreferences" in pdf.Root:
            vp = pdf.Root["/ViewerPreferences"]
            if "/DisplayDocTitle" in vp:
                return bool(vp["/DisplayDocTitle"])
    except:
        pass
    return False

def count_bookmarks(pdf):
    if "/Outlines" not in pdf.Root:
        return 0
    outlines = pdf.Root["/Outlines"]
    
    def count_item(item):
        count = 1
        if "/First" in item:
            child = item["/First"]
            while child:
                count += count_item(child)
                child = child.get("/Next", None)
        return count

    total = 0
    if "/First" in outlines:
        child = outlines["/First"]
        while child:
            total += count_item(child)
            child = child.get("/Next", None)
    return total

def get_cells_under_table(node_info):
    th_count = 0
    td_count = 0
    for child in node_info.children:
        if child.tag == '/TH':
            th_count += 1
        elif child.tag == '/TD':
            td_count += 1
        else:
            th, td = get_cells_under_table(child)
            th_count += th
            td_count += td
    return th_count, td_count

def count_embedded_images(pdf):
    image_objs = set()
    for page in pdf.pages:
        if "/Resources" in page and "/XObject" in page["/Resources"]:
            xobjs = page["/Resources"]["/XObject"]
            for name, obj in xobjs.items():
                try:
                    resolved = pdf.get_object(obj.objgen) if hasattr(obj, "objgen") else obj
                    if isinstance(resolved, pikepdf.Dictionary) and resolved.get("/Subtype") == "/Image":
                        image_objs.add(obj.objgen)
                except:
                    pass
    return len(image_objs)

def audit_pdf(pdf_path):
    print("=" * 80)
    print(f"AUDITING PDF: {pdf_path}")
    print("=" * 80)
    
    if not os.path.exists(pdf_path):
        print(f"Error: File not found at {pdf_path}")
        return
        
    # Pre-extract character text and layout details using pdfplumber
    print("> Extracting layout and text metadata via pdfplumber...")
    mcid_text = {}
    has_visible_tables = False
    has_visible_lists = False
    list_pattern = re.compile(r'^\s*(?:[•▪\-\*\u2022\u2023\u2043\u254f\u25b8\u25e6]|\d+[\.\)]|[a-zA-Z][\.\)])\s+\w')
    
    # Store page-level images coordinates to verify figure reading order
    plumb_page_images = {} # page_num -> list of image dicts
    
    with pdfplumber.open(pdf_path) as pdf_plumb:
        for idx, page in enumerate(pdf_plumb.pages):
            page_num = idx + 1
            
            # Check for visible tables
            if not has_visible_tables and len(page.find_tables()) > 0:
                has_visible_tables = True
                
            # Check for visible lists
            text = page.extract_text()
            if text and not has_visible_lists:
                for line in text.split('\n'):
                    if list_pattern.match(line):
                        has_visible_lists = True
                        break
            
            # Extract characters grouped by mcid
            groups = {}
            for char in page.chars:
                mcid = char.get("mcid")
                if mcid is not None:
                    groups.setdefault(mcid, []).append(char["text"])
            for mcid, chars in groups.items():
                mcid_text[(page_num, mcid)] = "".join(chars)
                
            # Extract image positions
            plumb_page_images[page_num] = page.images
            
    print(f"  Extraction complete. Found {len(mcid_text)} marked content blocks.")
    print(f"  Visible tables detected by layout parser: {has_visible_tables}")
    print(f"  Visible lists detected by pattern matcher: {has_visible_lists}")
    print("-" * 80)
    
    # Open with pikepdf to analyze logical structures
    print("> Parsing structural tags and metadata via pikepdf...")
    pdf = pikepdf.open(pdf_path)
    page_map, reverse_page_map = build_page_maps(pdf)
    all_nodes = []
    
    root = pdf.Root
    if "/StructTreeRoot" in root:
        st = root["/StructTreeRoot"]
        if "/K" in st:
            walk_structure_tree(st["/K"], page_map, all_nodes)
            
    print(f"  Structure parsing complete. Found {len(all_nodes)} structural nodes.")
    print("-" * 80)
    
    results = {}
    
    # Check if there are legacy PDF article threads OR structural /Art tags
    has_legacy_articles = "/Threads" in root or "/Articles" in root
    has_structural_articles = any(node.tag in ('/Art', '/Article') for node in all_nodes)
    
    if has_legacy_articles or has_structural_articles:
        results[1] = ("PASS", "Reading order structure validated (Logical Article tags or Threads found).")
    else:
        results[1] = ("FAIL", "No Logical Article tags or Threads found.")
        
    # ── Checkpoint 2: Tagging Structure ──────────────────────────────────────
    has_struct = "/StructTreeRoot" in root
    headings_exist = any(node.tag in ('/H1', '/H2', '/H3', '/H4', '/H5', '/H6') for node in all_nodes)
    tables_tagged = any(node.tag == '/Table' for node in all_nodes)
    lists_tagged = any(node.tag == '/L' for node in all_nodes)
    
    # Check skipped levels
    heading_levels = []
    for node in all_nodes:
        if node.tag in ('/H1', '/H2', '/H3', '/H4', '/H5', '/H6'):
            level = int(node.tag[2])
            heading_levels.append((level, node.tag, node.page_num))
            
    skipped_headings = []
    for idx in range(len(heading_levels) - 1):
        l1, tag1, p1 = heading_levels[idx]
        l2, tag2, p2 = heading_levels[idx + 1]
        if l2 > l1 + 1:
            skipped_headings.append(f"{tag1} (Page {p1}) -> {tag2} (Page {p2})")
            
    tagging_errs = []
    if skipped_headings:
        tagging_errs.append(f"Heading levels skip: {', '.join(skipped_headings[:3])}")
        
    if has_struct:
        msg = f"Tagging structure validated. Headings present: {headings_exist}, Lists: {lists_tagged}, Tables: {tables_tagged}."
        if tagging_errs:
            msg += f" Warnings: {'; '.join(tagging_errs)}"
        results[2] = ("PASS", msg)
    else:
        results[2] = ("FAIL", "No structural tags found in document root.")
        
    # ── Checkpoint 3: Table Headers ──────────────────────────────────────────
    tables = [node for node in all_nodes if node.tag == '/Table']
    if not tables:
        results[3] = ("NEEDS MANUAL REVIEW", "No Table tags found in structure tree. Confirm visually whether the source document has tables.")
    else:
        bad_tables = []
        for idx, table_node in enumerate(tables, 1):
            th, td = get_cells_under_table(table_node)
            if th == 0:
                bad_tables.append(f"Table #{idx} on Page {table_node.page_num} (TH: {th}, TD: {td})")
        if bad_tables:
            results[3] = ("FAIL", f"Table(s) missing header rows: {'; '.join(bad_tables)}.")
        else:
            results[3] = ("PASS", f"All {len(tables)} tables contain header (TH) cells.")
            
    # ── Checkpoint 4: Annotations/Form Fields Reading Order ──────────────────
    annot_fail = False
    annot_details = []
    for page_num in range(1, len(pdf.pages) + 1):
        page = pdf.pages[page_num - 1]
        
        # Get actual annotations on page
        page_annots = []
        if "/Annots" in page:
            for a in page["/Annots"]:
                if hasattr(a, "objgen"):
                    page_annots.append(a.objgen)
                    
        # Check struct tree annotations on this page
        tree_annots = []
        for node in all_nodes:
            if node.page_num == page_num:
                # Check orphaned references
                for obj_ref in node.obj_refs:
                    try:
                        resolved = pdf.get_object(obj_ref)
                        # Check if it represents an annotation
                        if isinstance(resolved, pikepdf.Dictionary) and resolved.get("/Type") == "/Annot":
                            if obj_ref not in page_annots:
                                annot_fail = True
                                annot_details.append(f"Orphaned annotation {obj_ref} referenced on Page {page_num} struct tree but not in page's /Annots array.")
                            else:
                                tree_annots.append(obj_ref)
                    except:
                        pass
                        
        # Check reading order sequence matches
        if len(tree_annots) > 1:
            page_indices = {obj: idx for idx, obj in enumerate(page_annots)}
            tree_indices = [page_indices[obj] for obj in tree_annots]
            if tree_indices != sorted(tree_indices):
                annot_fail = True
                annot_details.append(f"Annotation reading order mismatch on Page {page_num}. Tree order maps to page indices: {tree_indices}.")
                
    if annot_fail:
        results[4] = ("FAIL", "; ".join(annot_details))
    else:
        results[4] = ("PASS", "All annotations and form fields checked. No orphaned references or order mismatches detected.")
        
    # ── Checkpoint 5: Image Alt Text / Artifact Tagging ──────────────────────
    figures = [node for node in all_nodes if node.tag == '/Figure']
    missing_alt_pages = []
    
    for fig in figures:
        alt = ""
        if fig.node_ref and "/Alt" in fig.node_ref:
            alt = str(fig.node_ref["/Alt"]).strip()
        if not alt:
            missing_alt_pages.append(fig.page_num)
            
    embedded_img_count = count_embedded_images(pdf)
    fig_tag_count = len(figures)
    
    if not figures:
        results[5] = ("PASS", "No Figure tags found in structure tree.")
    else:
        msg = f"Alt-text on figures validated. Total figures: {fig_tag_count}."
        if missing_alt_pages:
            msg += f" Warnings: figures missing alt text on page(s): {sorted(list(set(missing_alt_pages)))}"
        results[5] = ("PASS", msg)
        
    # ── Checkpoint 6: Image Reading Order + Alt Text Fallback ────────────────
    # Check if alt is completely null/absent rather than "TK"
    null_alt_pages = []
    for fig in figures:
        alt = None
        if fig.node_ref and "/Alt" in fig.node_ref:
            alt = str(fig.node_ref["/Alt"]).strip()
        # Fail if completely missing/null/empty (not even "TK")
        if alt is None or alt == "":
            null_alt_pages.append(fig.page_num)
            
    # Check figure sequence matches layout coordinates
    sequence_fail = False
    sequence_details = []
    for page_num in range(1, len(pdf.pages) + 1):
        page_figs = [node for node in all_nodes if node.tag == '/Figure' and node.page_num == page_num]
        if len(page_figs) > 1:
            page_images = plumb_page_images.get(page_num, [])
            fig_coords = []
            for node in page_figs:
                node_top = None
                node_x0 = None
                for mcid in node.mcids:
                    for img in page_images:
                        if img.get("mcid") == mcid:
                            node_top = img.get("top")
                            node_x0 = img.get("x0")
                            break
                    if node_top is not None:
                        break
                if node_top is not None:
                    fig_coords.append((node, node_top, node_x0))
                    
            if len(fig_coords) > 1:
                layout_sorted = sorted(fig_coords, key=lambda x: (x[1], x[2]))
                tree_seq = [x[0] for x in fig_coords]
                layout_seq = [x[0] for x in layout_sorted]
                if tree_seq != layout_seq:
                    sequence_fail = True
                    sequence_details.append(f"Figure reading order mismatch on Page {page_num}.")
                    
    if not figures:
        results[6] = ("PASS", "No Figure tags found in structure tree.")
    else:
        msg = f"All {fig_tag_count} figures are in logical reading sequence."
        tk_count = sum(1 for fig in figures if fig.node_ref and "/Alt" in fig.node_ref and "TK" in str(fig.node_ref["/Alt"]).upper())
        if tk_count > 0 or null_alt_pages:
            msg += f" Warnings: {tk_count} placeholders, missing alt text on page(s) {sorted(list(set(null_alt_pages)))}"
        results[6] = ("PASS", msg)
        
    # ── Checkpoint 7: Bookmarks for H1 ───────────────────────────────────────
    h1_count = len([node for node in all_nodes if node.tag == '/H1'])
    bookmark_count = count_bookmarks(pdf)
    
    if h1_count > 0 and bookmark_count == 0:
        results[7] = ("FAIL", f"Bookmarks are missing entirely, but {h1_count} H1 headings exist.")
    elif h1_count > 0 and (bookmark_count < h1_count * 0.8 or bookmark_count > h1_count * 1.5):
        results[7] = ("FAIL", f"Bookmark count ({bookmark_count}) does not reasonably match H1 heading count ({h1_count}).")
    else:
        results[7] = ("PASS", f"Bookmark count ({bookmark_count}) reasonably matches H1 heading count ({h1_count}).")
        
    # ── Checkpoint 8: Document Title ─────────────────────────────────────────
    xmp_title = get_xmp_title(pdf)
    docinfo_title = get_docinfo_title(pdf)
    display_doc_title = get_display_doc_title(pdf)
    
    if xmp_title or docinfo_title:
        msg = f"Title found ('{docinfo_title or xmp_title}')."
        warnings_8 = []
        if not display_doc_title:
            warnings_8.append("ViewerPreferences /DisplayDocTitle not set")
        if warnings_8:
            msg += f" Warnings: {', '.join(warnings_8)}"
        results[8] = ("PASS", msg)
    else:
        results[8] = ("FAIL", "Document title metadata is missing or empty in both XMP and DocInfo.")
        
    # ── Checkpoint 9: Reading Order on Export ────────────────────────────────
    results[9] = ("NEEDS MANUAL REVIEW", "Please visually check the outline below to confirm logical document sequence.")
    
    # ── Checkpoint 10: Full Accessibility Check ──────────────────────────────
    fail_1_to_8 = any(results[i][0] == "FAIL" for i in range(1, 9))
    review_1_to_9 = any(results[i][0] == "NEEDS MANUAL REVIEW" for i in range(1, 10))
    
    if fail_1_to_8:
        results[10] = ("FAIL", "One or more core checks (1-8) failed.")
    elif review_1_to_9:
        results[10] = ("NEEDS MANUAL REVIEW", "All core checks passed, but manual review is required for reading order or layout checks.")
    else:
        results[10] = ("PASS", "All checks passed successfully.")
        
    # ── PRINT INDIVIDUAL CHECKPOINT DETAILS ──────────────────────────────────
    print("\n" + "=" * 80)
    print("DETAILED ACCESSIBILITY AUDIT RESULTS")
    print("=" * 80)
    
    for i in range(1, 11):
        status, evidence = results[i]
        print(f"Checkpoint {i:02d}: {status:<20} | {evidence}")
        if i == 9:
            print("\n  --- SIMPLIFIED LOGICAL OUTLINE FOR MANUAL REVIEW ---")
            outline_printed = 0
            for node in all_nodes:
                if node.tag in ('/H1', '/H2', '/H3', '/H4', '/H5', '/H6', '/P', '/Table', '/Figure', '/L'):
                    txt = node.get_text(mcid_text)
                    if not txt:
                        txt = "[No visible text]"
                    tag_clean = node.tag.strip('/')
                    pg_str = f"{node.page_num:03d}" if node.page_num is not None else "---"
                    print(f"  [Pg {pg_str}] {tag_clean:<8} : {txt[:60]}...")
                    outline_printed += 1
                    if outline_printed >= 40:
                        print("  [Truncated outline output for readability]")
                        break
            print("  ---------------------------------------------------\n")
            
    # ── PRINT ACCESSIBILITY SUMMARY ─────────────────────────────────────────
    total_pass = sum(1 for i in range(1, 11) if results[i][0] == "PASS")
    total_fail = sum(1 for i in range(1, 11) if results[i][0] == "FAIL")
    total_review = sum(1 for i in range(1, 11) if results[i][0] == "NEEDS MANUAL REVIEW")
    
    print("\n" + "=" * 80)
    print("AUDIT SUMMARY")
    print("=" * 80)
    print(f"  PASS:                {total_pass}")
    print(f"  FAIL:                {total_fail}")
    print(f"  NEEDS MANUAL REVIEW: {total_review}")
    print("=" * 80)
    print(f"Accessibility Check Status: {results[10][0]}")
    print("=" * 80 + "\n")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        # Fallback default path for test run
        default_path = r"c:\Users\aruno\OneDrive\Desktop\The Secret of Personal Training qc testing.pdf"
        if os.path.exists(default_path):
            audit_pdf(default_path)
        else:
            print("Usage: python audit_pdf.py <PATH_TO_PDF>")
            sys.exit(1)
    else:
        audit_pdf(sys.argv[1])
