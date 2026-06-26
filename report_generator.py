import io
import datetime
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.pdfgen import canvas

class NumberedCanvas(canvas.Canvas):
    """Custom canvas to support headers and two-pass page numbers ('Page X of Y')."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_elements(num_pages)
            super().showPage()
        super().save()

    def draw_page_elements(self, page_count):
        self.saveState()
        self.setFont("Helvetica-Bold", 8)
        self.setFillColor(colors.HexColor("#374151"))
        
        # Top Header (Only on page 2 and later, or all pages if needed. We'll show on all pages for consistency)
        self.drawString(54, 758, "PDF Accessibility Checker — Audit Report")
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#6B7280"))
        self.drawRightString(612 - 54, 758, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        
        # Header separator line
        self.setStrokeColor(colors.HexColor("#E5E7EB"))
        self.setLineWidth(0.75)
        self.line(54, 750, 612 - 54, 750)
        
        # Bottom Footer
        self.line(54, 54, 612 - 54, 54)
        self.setFont("Helvetica", 8)
        self.drawString(54, 40, "Confidential · Generated locally")
        page_text = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(612 - 54, 40, page_text)
        
        self.restoreState()


def generate_pdf_report(results, filename):
    """
    Generate a styled PDF audit report using ReportLab.
    Returns: bytes of the generated PDF.
    """
    buffer = io.BytesIO()
    
    # Page dimensions setup (Letter: 612 x 792 pt). Margins = 54 pt (0.75 inch)
    # Printable area: width = 504 pt, height = 684 pt
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=54,
        rightMargin=54,
        topMargin=64,
        bottomMargin=64
    )
    
    styles = getSampleStyleSheet()
    
    # Define custom styles
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=22,
        leading=26,
        textColor=colors.HexColor("#111827")
    )
    
    subtitle_style = ParagraphStyle(
        'DocSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#4B5563")
    )
    
    h2_style = ParagraphStyle(
        'SectionHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=14,
        leading=18,
        textColor=colors.HexColor("#1F2937"),
        spaceBefore=12,
        spaceAfter=8
    )
    
    body_style = ParagraphStyle(
        'BodyText',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9.5,
        leading=13.5,
        textColor=colors.HexColor("#374151")
    )
    
    check_title_style = ParagraphStyle(
        'CheckTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=10.5,
        leading=14,
        textColor=colors.HexColor("#111827")
    )
    
    badge_style_pass = ParagraphStyle(
        'BadgePass',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=12,
        alignment=1, # Center
        textColor=colors.HexColor("#15803D")
    )
    
    badge_style_fail = ParagraphStyle(
        'BadgeFail',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=12,
        alignment=1, # Center
        textColor=colors.HexColor("#B91C1C")
    )
    
    loc_title_style = ParagraphStyle(
        'LocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#374151")
    )
    
    loc_desc_style = ParagraphStyle(
        'LocDesc',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#4B5563")
    )
    
    story = []
    
    # 1. Header Information
    story.append(Spacer(1, 10))
    story.append(Paragraph("Accessibility Audit Report", title_style))
    story.append(Spacer(1, 4))
    story.append(Paragraph(f"<b>File:</b> {filename} &nbsp;&nbsp;|&nbsp;&nbsp; <b>Checked on:</b> {datetime.datetime.now().strftime('%B %d, %Y at %H:%M')}", subtitle_style))
    story.append(Spacer(1, 18))
    
    # Calculate statistics
    pass_count = sum(1 for r in results if r.get("status") == "PASS")
    fail_count = sum(1 for r in results if r.get("status") == "FAIL")
    score = int(round((pass_count / len(results)) * 100)) if results else 0
    
    # 2. Summary cards (Passed, Compliance Score, Failed)
    center_style = ParagraphStyle('CenterStyle', parent=styles['Normal'], alignment=1)
    
    summary_data = [
        [
            Paragraph("<para align=center><font size=24 color='#16A34A'><b>{}</b></font><br/><font size=8.5 color='#4B5563'>Passed Checks</font></para>".format(pass_count), center_style),
            Paragraph("<para align=center><font size=24 color='#2563EB'><b>{}%</b></font><br/><font size=8.5 color='#4B5563'>Accessibility Score</font></para>".format(score), center_style),
            Paragraph("<para align=center><font size=24 color='#DC2626'><b>{}</b></font><br/><font size=8.5 color='#4B5563'>Failed Checks</font></para>".format(fail_count), center_style),
        ]
    ]
    summary_table = Table(summary_data, colWidths=[168, 168, 168])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (0,0), colors.HexColor('#F0FDF4')),
        ('BACKGROUND', (1,0), (1,0), colors.HexColor('#EFF6FF')),
        ('BACKGROUND', (2,0), (2,0), colors.HexColor('#FEF2F2')),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('BOX', (0,0), (0,0), 1, colors.HexColor('#DCFCE7')),
        ('BOX', (1,0), (1,0), 1, colors.HexColor('#DBEAFE')),
        ('BOX', (2,0), (2,0), 1, colors.HexColor('#FEE2E2')),
        ('TOPPADDING', (0,0), (-1,-1), 10),
        ('BOTTOMPADDING', (0,0), (-1,-1), 10),
    ]))
    
    story.append(summary_table)
    story.append(Spacer(1, 16))
    
    # 3. Detailed Results Section
    story.append(Paragraph("Detailed Audit Checklist", h2_style))
    story.append(Spacer(1, 6))
    
    # Loop over the 10 checks
    for idx, r in enumerate(results):
        check_id = r.get("id", idx + 1)
        check_name = r.get("name", "Unknown Check")
        status = r.get("status", "FAIL")
        detail = r.get("detail", "")
        locations = r.get("locations", [])
        
        # Format the check status badge
        badge_text = "PASS" if status == "PASS" else "FAIL"
        badge_p = Paragraph(f"<b>{badge_text}</b>", badge_style_pass if status == "PASS" else badge_style_fail)
        
        # Check Title block
        title_p = Paragraph(f"<b>{check_id:02d}. {check_name}</b>", check_title_style)
        
        # Grid layout for Check title and badge
        # Width of page is 504 pt. Title takes 420 pt, Badge takes 84 pt.
        title_table = Table([[title_p, badge_p]], colWidths=[420, 84])
        title_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#F9FAFB') if idx % 2 == 0 else colors.HexColor('#FFFFFF')),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('BOTTOMPADDING', (0,0), (-1,-1), 6),
            ('TOPPADDING', (0,0), (-1,-1), 6),
            ('LEFTPADDING', (0,0), (-1,-1), 8),
            ('RIGHTPADDING', (0,0), (-1,-1), 8),
            ('BOX', (0,0), (-1,-1), 0.5, colors.HexColor('#F3F4F6')),
        ]))
        
        # Check detail text
        detail_p = Paragraph(detail, body_style)
        
        check_flowables = [
            title_table,
            Spacer(1, 4),
            Paragraph(f"<font color='#4B5563'>{detail}</font>", body_style),
        ]
        
        # If check failed and has locations, add locations block
        if status == "FAIL" and locations:
            loc_flowables = []
            loc_flowables.append(Spacer(1, 4))
            
            loc_header = Paragraph("<b>Error Locations & Issues Found:</b>", ParagraphStyle('LocHeader', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=8.5, leading=11, textColor=colors.HexColor('#991B1B')))
            loc_flowables.append(loc_header)
            loc_flowables.append(Spacer(1, 4))
            
            # Format locations inside a light-red background table
            loc_table_rows = []
            for loc in locations:
                label_p = Paragraph(f"📍 {loc.get('label', 'Unknown Location')}", loc_title_style)
                desc_p = Paragraph(loc.get('description', ''), loc_desc_style)
                loc_table_rows.append([label_p, desc_p])
                
            loc_table = Table(loc_table_rows, colWidths=[130, 350])
            loc_table.setStyle(TableStyle([
                ('VALIGN', (0,0), (-1,-1), 'TOP'),
                ('BOTTOMPADDING', (0,0), (-1,-1), 4),
                ('TOPPADDING', (0,0), (-1,-1), 4),
                ('LEFTPADDING', (0,0), (-1,-1), 6),
                ('RIGHTPADDING', (0,0), (-1,-1), 6),
                ('LINEBELOW', (0,0), (-2,-2), 0.25, colors.HexColor('#FCA5A5')),
            ]))
            
            # Inner container table for formatting
            loc_container = Table([[loc_table]], colWidths=[490])
            loc_container.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#FEF2F2')),
                ('BOX', (0,0), (-1,-1), 0.75, colors.HexColor('#FEE2E2')),
                ('TOPPADDING', (0,0), (-1,-1), 6),
                ('BOTTOMPADDING', (0,0), (-1,-1), 6),
                ('LEFTPADDING', (0,0), (-1,-1), 6),
                ('RIGHTPADDING', (0,0), (-1,-1), 6),
            ]))
            
            loc_flowables.append(loc_container)
            check_flowables.extend(loc_flowables)
            
        check_flowables.append(Spacer(1, 12))
        story.append(KeepTogether(check_flowables))
        
    # Build PDF doc using custom NumberedCanvas
    doc.build(story, canvasmaker=NumberedCanvas)
    
    pdf_data = buffer.getvalue()
    buffer.close()
    return pdf_data
