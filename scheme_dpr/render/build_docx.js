// Render the DPR block list (JSON written by scheme_dpr/report.py) to .docx.
// Usage: node build_docx.js blocks.json out.docx   (needs the "docx" npm package)
"use strict";
const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, Table, TableRow, TableCell,
  WidthType, AlignmentType, BorderStyle, ShadingType, PageBreak, Footer, PageNumber,
  LevelFormat,
} = require("docx");

const [, , inPath, outPath] = process.argv;
if (!inPath || !outPath) {
  console.error("usage: node build_docx.js blocks.json out.docx");
  process.exit(2);
}
const doc = JSON.parse(fs.readFileSync(inPath, "utf8"));

const FONT = "Calibri";
const PAGE_WIDTH_TWIPS = 9638; // A4 minus 2 cm margins each side
const BORDER = { style: BorderStyle.SINGLE, size: 4, color: "BFBFBF" };
const CELL_BORDERS = { top: BORDER, bottom: BORDER, left: BORDER, right: BORDER };

const HEADINGS = { 0: HeadingLevel.TITLE, 1: HeadingLevel.HEADING_1, 2: HeadingLevel.HEADING_2, 3: HeadingLevel.HEADING_3 };

function para(block) {
  const style = block.style || "normal";
  const run = { text: block.text, font: FONT, size: 21 };
  if (style === "warning") Object.assign(run, { bold: true, color: "C00000" });
  if (style === "note") Object.assign(run, { italics: true, color: "595959", size: 19 });
  if (style === "subtitle") Object.assign(run, { color: "1F4E79", size: 28 });
  const opts = { children: [new TextRun(run)], spacing: { after: 120 } };
  if (style === "warning") {
    opts.border = { left: { style: BorderStyle.SINGLE, size: 18, color: "C00000", space: 8 } };
    opts.indent = { left: 200 };
  }
  return new Paragraph(opts);
}

function cell(text, { bold = false, header = false, align = "left", width }) {
  return new TableCell({
    borders: CELL_BORDERS,
    width: { size: width, type: WidthType.DXA },
    shading: header ? { type: ShadingType.CLEAR, fill: "DDEBF7", color: "auto" } : undefined,
    margins: { top: 40, bottom: 40, left: 80, right: 80 },
    children: [new Paragraph({
      alignment: align === "right" ? AlignmentType.RIGHT : AlignmentType.LEFT,
      children: [new TextRun({ text: String(text ?? ""), bold: bold || header, font: FONT, size: 18 })],
    })],
  });
}

function table(block) {
  const ncol = (block.header && block.header.length) || (block.rows[0] || []).length || 1;
  let widths;
  if (block.key_value) {
    widths = [Math.round(PAGE_WIDTH_TWIPS * 0.38), PAGE_WIDTH_TWIPS - Math.round(PAGE_WIDTH_TWIPS * 0.38)];
  } else if (ncol > 2 && block.align && block.align.slice(1).every((a) => a === "right")) {
    const first = Math.round(PAGE_WIDTH_TWIPS * 0.34);
    const rest = Math.floor((PAGE_WIDTH_TWIPS - first) / (ncol - 1));
    widths = [first, ...Array(ncol - 1).fill(rest)];
  } else {
    widths = Array(ncol).fill(Math.floor(PAGE_WIDTH_TWIPS / ncol));
  }
  const align = block.align || [];
  const bold = new Set(block.bold_rows || []);
  const rows = [];
  if (block.header && block.header.length) {
    rows.push(new TableRow({ tableHeader: true, children: block.header.map((h, i) => cell(h, { header: true, align: align[i], width: widths[i] })) }));
  }
  block.rows.forEach((r, ri) => {
    rows.push(new TableRow({
      children: r.map((c, i) => cell(c, { bold: bold.has(ri) || (block.key_value && i === 0), align: align[i], width: widths[i] })),
    }));
  });
  return new Table({ width: { size: PAGE_WIDTH_TWIPS, type: WidthType.DXA }, columnWidths: widths, rows });
}

const children = [];
for (const b of doc.blocks) {
  if (b.type === "heading") {
    children.push(new Paragraph({ heading: HEADINGS[b.level] ?? HeadingLevel.HEADING_3, children: [new TextRun({ text: b.text })], spacing: { before: 240, after: 120 } }));
  } else if (b.type === "para") {
    children.push(para(b));
  } else if (b.type === "bullets") {
    for (const item of b.items) {
      children.push(new Paragraph({ numbering: { reference: "bullets", level: 0 }, children: [new TextRun({ text: item, font: FONT, size: 20 })] }));
    }
  } else if (b.type === "checklist") {
    for (const item of b.items) {
      children.push(new Paragraph({ indent: { left: 360 }, spacing: { after: 60 }, children: [new TextRun({ text: "☐  " + item, font: FONT, size: 20 })] }));
    }
  } else if (b.type === "table") {
    children.push(table(b));
    children.push(new Paragraph({ children: [], spacing: { after: 120 } }));
  } else if (b.type === "pagebreak") {
    children.push(new Paragraph({ children: [new PageBreak()] }));
  }
}

const document = new Document({
  creator: "scheme-dpr",
  title: doc.title,
  styles: {
    default: { document: { run: { font: FONT, size: 21 } } },
    paragraphStyles: [
      { id: "Title", name: "Title", basedOn: "Normal", run: { size: 40, bold: true, color: "1F4E79", font: FONT } },
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true, run: { size: 30, bold: true, color: "1F4E79", font: FONT }, paragraph: { spacing: { before: 280, after: 120 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true, run: { size: 24, bold: true, color: "2E75B6", font: FONT }, paragraph: { spacing: { before: 200, after: 100 }, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true, run: { size: 21, bold: true, color: "404040", font: FONT }, paragraph: { spacing: { before: 160, after: 80 }, outlineLevel: 2 } },
    ],
  },
  numbering: {
    config: [{ reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 270 } } } }] }],
  },
  sections: [{
    properties: { page: { size: { width: 11906, height: 16838 }, margin: { top: 1134, bottom: 1134, left: 1134, right: 1134 } } },
    footers: {
      default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [
        new TextRun({ text: "DRAFT - human review required before submission  ·  page ", size: 16, color: "808080" }),
        new TextRun({ children: [PageNumber.CURRENT], size: 16, color: "808080" }),
      ] })] }),
    },
    children,
  }],
});

Packer.toBuffer(document).then((buf) => fs.writeFileSync(outPath, buf)).catch((e) => { console.error(e); process.exit(1); });
