from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from jobintel.matching.cv_document import CVDocument, DatedEntry

# A professional, conventional UK graduate CV layout:
# Header -> Professional Summary -> Education -> Technical Skills -> Key Projects
# -> Internship Experience. Real pagination via reportlab's Platypus text flow:
# SimpleDocTemplate.build() does actual word-wrapping/page-breaking against the
# real font metrics, `.page` after build() is the true page count, and the frame
# cursor after the last flowable gives the real vertical page utilisation.
# "Fits one page" means the real rendered document is one page -- and
# utilisation says whether that page is actually used.


@dataclass(frozen=True)
class LayoutPreset:
    """One of a small, bounded set of professional page geometries. The fitter
    steps through them in order instead of shrinking content continuously."""

    name: str
    margin_mm: float
    body_pt: float


LAYOUT_PRESETS = (
    LayoutPreset("normal", margin_mm=18.0, body_pt=10.0),
    LayoutPreset("dense", margin_mm=15.0, body_pt=10.0),
    # Densest standard preset: comparable to a candidate-authored one-page engineering CV
    LayoutPreset("compact", margin_mm=12.5, body_pt=9.5),
    # Narrower bounded preset: 10mm margins for maximum content density while maintaining font size >= 9.5pt
    LayoutPreset("narrow", margin_mm=10.0, body_pt=9.5),
)
NORMAL_LAYOUT = LAYOUT_PRESETS[0]


@dataclass(frozen=True)
class RenderResult:
    pdf_path: Path
    page_count: int
    margin_mm: float
    body_pt: float
    # Fraction (0-1) of the last page's usable frame height occupied by content.
    page_utilization: float
    layout_preset: str = NORMAL_LAYOUT.name


class _MeasuringDocTemplate(SimpleDocTemplate):
    last_frame_fill = 0.0

    def afterFlowable(self, flowable) -> None:  # noqa: N802 (reportlab hook name)
        frame = self.frame
        top = frame._y1 + frame._height - frame._topPadding
        usable = top - frame._y1p
        self.last_frame_fill = max(0.0, min(1.0, (top - frame._y) / usable)) if usable > 0 else 0.0


def render_cv_document(path: Path, doc: CVDocument, preset: LayoutPreset = NORMAL_LAYOUT) -> RenderResult:
    margin_mm, body_pt = preset.margin_mm, preset.body_pt
    path.parent.mkdir(parents=True, exist_ok=True)
    styles = _styles(body_pt)
    template = _MeasuringDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=margin_mm * mm,
        rightMargin=margin_mm * mm,
        topMargin=margin_mm * mm,
        bottomMargin=margin_mm * mm,
        title=f"{doc.name} - CV",
    )
    # Platypus frames pad their content by 6pt on each side.
    width = A4[0] - 2 * margin_mm * mm - 12

    header = f"<b>{escape(doc.name)}</b>" + (f" | {escape(doc.headline)}" if doc.headline else "")
    story: list = [Paragraph(header, styles["name"])]
    if doc.contact_line:
        story.append(Paragraph(escape(doc.contact_line), styles["contact"]))

    if doc.summary:
        story += _heading("PROFESSIONAL SUMMARY", styles)
        story.append(Paragraph(escape(doc.summary), styles["body"]))

    if doc.education:
        story += _heading("EDUCATION", styles)
        for entry in doc.education:
            story.append(_dated_row(entry, styles, width))
            story.extend(Paragraph(escape(detail), styles["detail"]) for detail in entry.details)

    groups = [group for group in doc.skill_groups if group.skills]
    if groups:
        story += _heading("TECHNICAL SKILLS", styles)
        for group in groups:
            story.append(Paragraph(f"<b>{escape(group.label)}:</b> {escape(', '.join(group.skills))}", styles["body"]))

    if doc.projects:
        story += _heading("KEY PROJECTS", styles)
        for project in doc.projects:
            line = f"<b>{escape(project.name)}</b>"
            if project.technologies:
                line += f" | <i>{escape(', '.join(project.technologies))}</i>"
            story.append(Paragraph(line, styles["subheading"]))
            story.extend(Paragraph(escape(bullet.text), styles["bullet"], bulletText="•") for bullet in project.bullets)

    if doc.experience:
        story += _heading("INTERNSHIP EXPERIENCE", styles)
        for entry in doc.experience:
            story.append(_dated_row(entry, styles, width))
            story.extend(Paragraph(escape(detail), styles["bullet"], bulletText="•") for detail in entry.details)

    template.build(story)
    return RenderResult(
        pdf_path=path,
        page_count=template.page,
        margin_mm=margin_mm,
        body_pt=body_pt,
        page_utilization=round(template.last_frame_fill, 3),
        layout_preset=preset.name,
    )


def _heading(title: str, styles: dict[str, ParagraphStyle]) -> list:
    return [
        Spacer(1, 3),
        Paragraph(title, styles["heading"]),
        HRFlowable(width="100%", thickness=0.6, color="#444444", spaceBefore=0, spaceAfter=2),
    ]


def _dated_row(entry: DatedEntry, styles: dict[str, ParagraphStyle], width: float):
    heading = Paragraph(f"<b>{escape(entry.heading)}</b>", styles["subheading"])
    if not entry.dates:
        return heading
    dates_style = styles["dates"]
    dates = Paragraph(escape(entry.dates), dates_style)
    dates_width = min(width * 0.4, stringWidth(entry.dates, dates_style.fontName, dates_style.fontSize) + 8)
    table = Table([[heading, dates]], colWidths=[width - dates_width, dates_width], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return table


def _styles(body_pt: float) -> dict[str, ParagraphStyle]:
    leading = body_pt + 2.5
    return {
        "name": ParagraphStyle("name", fontName="Helvetica", fontSize=body_pt + 5, leading=body_pt + 8, alignment=1),
        "contact": ParagraphStyle("contact", fontName="Helvetica", fontSize=body_pt - 0.5, leading=body_pt + 2, alignment=1, textColor="#333333"),
        "heading": ParagraphStyle("heading", fontName="Helvetica-Bold", fontSize=body_pt + 0.5, leading=body_pt + 3, spaceBefore=2, textColor="#1a1a1a"),
        "subheading": ParagraphStyle("subheading", fontName="Helvetica", fontSize=body_pt, leading=leading, spaceBefore=2),
        "dates": ParagraphStyle("dates", fontName="Helvetica", fontSize=body_pt - 0.5, leading=leading, alignment=2, textColor="#333333"),
        "body": ParagraphStyle("body", fontName="Helvetica", fontSize=body_pt, leading=leading),
        "detail": ParagraphStyle("detail", fontName="Helvetica", fontSize=body_pt - 0.5, leading=leading - 0.5, textColor="#222222"),
        "bullet": ParagraphStyle("bullet", fontName="Helvetica", fontSize=body_pt, leading=leading, leftIndent=11, bulletIndent=2, bulletFontSize=body_pt),
    }
