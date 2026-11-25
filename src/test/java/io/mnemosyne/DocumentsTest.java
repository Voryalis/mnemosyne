package io.mnemosyne;

import org.apache.pdfbox.pdmodel.PDDocument;
import org.apache.pdfbox.pdmodel.PDPage;
import org.apache.pdfbox.pdmodel.PDPageContentStream;
import org.apache.pdfbox.pdmodel.font.PDType1Font;
import org.apache.pdfbox.pdmodel.font.Standard14Fonts;
import org.apache.poi.xssf.usermodel.XSSFWorkbook;
import org.apache.poi.xwpf.usermodel.XWPFDocument;
import org.junit.jupiter.api.Test;

import java.io.ByteArrayOutputStream;

import static org.junit.jupiter.api.Assertions.*;

class DocumentsTest {
    @Test
    void pdfChunksPreservePhysicalPageNumbers() throws Exception {
        var bytes = new ByteArrayOutputStream();
        try (var pdf = new PDDocument()) {
            for (String text : new String[]{"first page", "second page"}) {
                var page = new PDPage();
                pdf.addPage(page);
                try (var stream = new PDPageContentStream(pdf, page)) {
                    stream.beginText();
                    stream.setFont(new PDType1Font(Standard14Fonts.FontName.HELVETICA), 12);
                    stream.newLineAtOffset(20, 700);
                    stream.showText(text);
                    stream.endText();
                }
            }
            pdf.save(bytes);
        }
        var first = Documents.extract(bytes.toByteArray(), Documents.type("", "source.PDF"), 1, 0, 100);
        assertEquals(2, first.get("nextPage"));
        assertEquals("first page", first.get("text").toString().strip());
        var part = Documents.extract(bytes.toByteArray(), Documents.type("", "source.pdf"), 2, 0, 6);
        assertEquals("second", part.get("text"));
        assertEquals(6, part.get("nextOffset"));
        assertNull(part.get("nextPage"));
    }

    @Test
    void wordExtractionIncludesTableContent() throws Exception {
        var bytes = new ByteArrayOutputStream();
        try (var doc = new XWPFDocument()) {
            doc.createParagraph().createRun().setText("Opening");
            doc.createTable(1, 1).getRow(0).getCell(0).setText("Evidence");
            doc.write(bytes);
        }
        var result = Documents.extract(bytes.toByteArray(), Documents.type("", "memo.docx"), 1, 0, 100);
        assertEquals("Opening\nEvidence\t\n", result.get("text"));
    }

    @Test
    void spreadsheetReportsCoordinatesAndUsesCachedFormulaValues() throws Exception {
        var bytes = new ByteArrayOutputStream();
        try (var book = new XSSFWorkbook()) {
            var row = book.createSheet("Data").createRow(1);
            row.createCell(0).setCellValue(2);
            var formula = row.createCell(1);
            formula.setCellFormula("A2*2");
            book.getCreationHelper().createFormulaEvaluator().evaluateFormulaCell(formula);
            book.write(bytes);
        }
        var result = Documents.extract(bytes.toByteArray(), Documents.type("", "data.xlsx"), 1, 0, 100);
        assertEquals("Sheet: Data\nRow 2\tA2=2\tB2=4\n", result.get("text"));
    }

    @Test
    void cursorCannotSplitASupplementaryCharacter() {
        var first = Documents.chunk("A𐐀B", 0, 2);
        assertEquals("A", first.get("text"));
        var second = Documents.chunk("A𐐀B", (int) first.get("nextOffset"), 2);
        assertEquals("𐐀", second.get("text"));
        assertEquals(3, second.get("nextOffset"));
    }
}
