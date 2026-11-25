package io.mnemosyne;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.Writer;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

import org.apache.pdfbox.Loader;
import org.apache.pdfbox.text.PDFTextStripper;
import org.apache.poi.openxml4j.util.ZipSecureFile;
import org.apache.poi.ss.usermodel.DataFormatter;
import org.apache.poi.xssf.usermodel.XSSFWorkbook;
import org.apache.poi.xwpf.usermodel.XWPFDocument;
import org.apache.poi.xwpf.usermodel.XWPFParagraph;
import org.apache.poi.xwpf.usermodel.XWPFTable;

final class Documents {
    private static final int MAX_SKIP = 5_000_000;
    private static final String PDF = "application/pdf";
    private static final String DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
    private static final String XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

    static {
        ZipSecureFile.setMaxEntrySize(64L * 1024 * 1024);
        ZipSecureFile.setMaxFileCount(1000);
    }

    private Documents() {}

    static String type(String mime, String filename) throws IOException {
        String lower = filename.toLowerCase(Locale.ROOT);
        if (PDF.equals(mime) || lower.endsWith(".pdf")) return PDF;
        if (DOCX.equals(mime) || lower.endsWith(".docx")) return DOCX;
        if (XLSX.equals(mime) || lower.endsWith(".xlsx")) return XLSX;
        throw new IOException("Supported attachment types are PDF, DOCX and XLSX");
    }

    static Map<String, Object> extract(byte[] bytes, String type, int page, int offset, int limit) throws IOException {
        var capture = new Capture(offset, limit);
        var locations = new LinkedHashMap<String, Object>();
        try {
            switch (type) {
                case PDF -> {
                    try (var pdf = Loader.loadPDF(bytes)) {
                        if (page > pdf.getNumberOfPages()) {
                            throw new IOException("PDF page is outside the document");
                        }
                        locations.put("page", page);
                        locations.put("totalPages", pdf.getNumberOfPages());
                        var stripper = new PDFTextStripper();
                        stripper.setStartPage(page);
                        stripper.setEndPage(page);
                        stripper.writeText(pdf, capture);
                    }
                }
                case DOCX -> {
                    try (var document = new XWPFDocument(new ByteArrayInputStream(bytes))) {
                        for (var element : document.getBodyElements()) {
                            if (element instanceof XWPFParagraph paragraph) {
                                capture.line(paragraph.getText());
                            } else if (element instanceof XWPFTable table) {
                                for (var row : table.getRows()) {
                                    for (var cell : row.getTableCells()) {
                                        capture.write(cell.getText());
                                        capture.write("\t");
                                    }
                                    capture.write("\n");
                                }
                            }
                        }
                    }
                }
                case XLSX -> {
                    try (var workbook = new XSSFWorkbook(new ByteArrayInputStream(bytes))) {
                        var formatter = new DataFormatter(Locale.ROOT);
                        formatter.setUseCachedValuesForFormulaCells(true);
                        for (var sheet : workbook) {
                            capture.line("Sheet: " + sheet.getSheetName());
                            for (var row : sheet) {
                                capture.write("Row " + (row.getRowNum() + 1));
                                for (var cell : row) {
                                    capture.write("\t" + cell.getAddress() + "=" + formatter.formatCellValue(cell));
                                }
                                capture.write("\n");
                            }
                        }
                    }
                }
                default -> throw new IOException("Unsupported attachment type");
            }
        } catch (ChunkComplete complete) {
            // The writer already captured the requested window and its lookahead.
        }
        locations.putAll(capture.result());
        if (PDF.equals(type)) {
            locations.put("nextPage", capture.nextOffset() == null && page < (int) locations.get("totalPages")
                    ? page + 1 : null);
        }
        return locations;
    }

    static Map<String, Object> chunk(String value, int offset, int limit) {
        var capture = new Capture(offset, limit);
        try {
            capture.write(value);
        } catch (ChunkComplete complete) {
            // The writer stopped after recording lookahead.
        } catch (IOException unexpected) {
            throw new IllegalStateException(unexpected);
        }
        return capture.result();
    }

    private static final class Capture extends Writer {
        private final int offset;
        private final int limit;
        private final StringBuilder text = new StringBuilder();
        private int seen;

        Capture(int offset, int limit) {
            if (offset < 0 || offset > MAX_SKIP || limit < 1 || limit > 16_000) {
                throw new IllegalArgumentException("Invalid text offset or chunk size");
            }
            this.offset = offset;
            this.limit = limit;
        }

        @Override
        public void write(char[] data, int start, int length) throws IOException {
            int skip = Math.min(length, Math.max(0, offset - seen));
            int take = Math.min(length - skip, limit + 2 - text.length());
            text.append(data, start + skip, take);
            seen += skip + take;
            if (text.length() == limit + 2) {
                throw new ChunkComplete();
            }
        }

        void line(String value) throws IOException {
            write(value);
            write("\n");
        }

        int end() {
            int end = Math.min(text.length(), limit);
            if (end > 0 && end < text.length() && Character.isHighSurrogate(text.charAt(end - 1))) {
                return end == 1 ? 2 : end - 1;
            }
            return end;
        }

        Integer nextOffset() {
            return text.length() > end() && offset + end() <= MAX_SKIP ? offset + end() : null;
        }

        Map<String, Object> result() {
            var result = new LinkedHashMap<String, Object>();
            result.put("text", text.substring(0, end()));
            result.put("offset", offset);
            result.put("nextOffset", nextOffset());
            result.put("truncated", text.length() > end() && nextOffset() == null);
            return result;
        }

        @Override public void flush() {}
        @Override public void close() {}
    }

    private static final class ChunkComplete extends IOException {}
}
