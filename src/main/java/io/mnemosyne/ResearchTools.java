package io.mnemosyne;

import io.modelcontextprotocol.server.McpServerFeatures.SyncToolSpecification;
import io.modelcontextprotocol.spec.McpSchema.CallToolResult;
import io.modelcontextprotocol.spec.McpSchema.TextContent;
import io.modelcontextprotocol.spec.McpSchema.Tool;
import io.modelcontextprotocol.spec.McpSchema.ToolAnnotations;
import org.jsoup.Jsoup;
import tools.jackson.databind.JsonNode;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

final class ResearchTools {
    private static final Map<String, Object> STRING = Map.of("type", "string");
    private static final Map<String, Object> INTEGER = Map.of("type", "integer");
    private static final Map<String, Object> LIBRARY = Map.of("library",
            Map.of("type", "string", "description", "Configured library from list_libraries"));
    private final Zotero zotero;
    private final Map<String, String> libraries;

    ResearchTools(Zotero zotero, Map<String, String> libraries) {
        this.zotero = zotero;
        this.libraries = libraries;
    }

    List<SyncToolSpecification> specifications() {
        Map<String, Object> paging = Map.of("start", INTEGER, "limit", INTEGER);
        Map<String, Object> item = Map.of("key", STRING);
        Map<String, Object> chunks = Map.of("offset", INTEGER, "chars", INTEGER);
        return List.of(
                tool("list_libraries", "List the configured Zotero libraries.", false, Map.of(), List.of(),
                        args -> Map.of("libraries", libraries.keySet().stream().sorted().toList())),
                tool("search", "Search Zotero titles, creators and years. Use returned IDs with fetch.", true,
                        fields(paging, Map.of("query", STRING)), List.of("query"), this::search),
                tool("fetch", "Fetch a Zotero search result with text and citation metadata.", false,
                        Map.of("id", STRING), List.of("id"), this::fetch),
                tool("list_references", "List top-level bibliographic items. Optionally filter by collection.",
                        true, fields(paging, Map.of("collection", STRING)), List.of(), this::references),
                tool("list_collections", "List Zotero collections. Use parent collection keys to reconstruct the tree.",
                        true, paging, List.of(), args -> page(zotero.page(library(args) + "/collections",
                                Map.of(), start(args), limit(args)))),
                tool("get_reference", "Get the complete Zotero metadata for an item.", true, item, List.of("key"),
                        args -> zotero.json(itemPath(args))),
                tool("list_attachments", "List synchronized and linked attachments for a reference.",
                        true, fields(item, paging), List.of("key"), args -> children(args, "attachment")),
                tool("read_attachment", "Read a text chunk from a PDF page, DOCX body or XLSX workbook.",
                        true, fields(fields(item, chunks), Map.of("page", INTEGER)), List.of("key"),
                        this::attachment),
                tool("list_notes", "List research notes in a library or under a parent Zotero item.",
                        true, fields(paging, Map.of("parent", STRING)), List.of(), args -> children(args, "note")),
                tool("read_note", "Read plain text from a Zotero note with optional offset and chunk size.",
                        true, fields(item, chunks), List.of("key"), this::note),
                tool("search_full_text", "Run Zotero's everything search, including available indexed text.",
                        true, fields(paging, Map.of("query", STRING)), List.of("query"),
                        args -> page(zotero.page(library(args) + "/items",
                                Map.of("q", required(args, "query"), "qmode", "everything"),
                                start(args), limit(args)))),
                tool("read_indexed_text", "Read Zotero's synced text index for an attachment, if available.",
                        true, fields(item, chunks), List.of("key"), this::indexed),
                tool("detect_duplicates", "Find candidate duplicates by DOI, URL and normalized title in a bounded scan.",
                        true, paging, List.of(), this::duplicates),
                tool("export_bibtex", "Export a page of references or a single item as Zotero-generated BibTeX.",
                        true, fields(paging, item), List.of(), this::bibtex),
                tool("get_source", "Get citation metadata and the parent reference for a note or attachment.",
                        true, item, List.of("key"), args -> source(library(args), zotero.json(itemPath(args))))
        );
    }

    private Object search(Map<String, Object> args) throws IOException, InterruptedException {
        String library = alias(args);
        var result = zotero.page(libraries.get(library) + "/items/top",
                Map.of("q", required(args, "query"), "itemType", "-attachment || note"),
                start(args), limit(args));
        var results = new ArrayList<Map<String, String>>();
        for (var item : result.items()) {
            results.add(Map.of("id", library + "/" + item.path("key").asText(),
                    "title", title(item), "url", sourceUrl(item)));
        }
        var output = new LinkedHashMap<String, Object>();
        output.put("results", results);
        output.put("nextStart", result.nextStart());
        return output;
    }

    private Object fetch(Map<String, Object> args) throws IOException, InterruptedException {
        String id = required(args, "id");
        String[] parts = id.split("/", -1);
        if (parts.length != 2 || !libraries.containsKey(parts[0])) {
            throw new IllegalArgumentException("Use an ID returned by search");
        }
        String library = libraries.get(parts[0]);
        var item = zotero.json(library + "/items/" + key(parts[1]));
        String text = Zotero.JSON.writeValueAsString(item.path("data"));
        return Map.of("id", id, "title", title(item), "url", sourceUrl(item),
                "text", text, "metadata", source(library, item));
    }

    private Object references(Map<String, Object> args) throws IOException, InterruptedException {
        String path = library(args);
        if (args.containsKey("collection")) {
            path += "/collections/" + key(required(args, "collection"));
        }
        return page(zotero.page(path + "/items/top", Map.of("itemType", "-attachment || note"),
                start(args), limit(args)));
    }

    private Object children(Map<String, Object> args, String type) throws IOException, InterruptedException {
        String path = library(args) + "/items";
        if ("attachment".equals(type)) {
            path = itemPath(args) + "/children";
        } else if (args.containsKey("parent")) {
            path += "/" + key(required(args, "parent")) + "/children";
        }
        var result = zotero.page(path, Map.of("itemType", type), start(args), limit(args));
        var summaries = new ArrayList<Map<String, String>>();
        for (var item : result.items()) {
            var data = item.path("data");
            summaries.add(Map.of("key", item.path("key").asText(), "title", title(item),
                    "parentItem", data.path("parentItem").asText(), "filename", data.path("filename").asText(),
                    "contentType", data.path("contentType").asText(), "linkMode", data.path("linkMode").asText()));
        }
        return page(new Zotero.Page(Zotero.JSON.valueToTree(summaries), result.nextStart(), result.total()));
    }

    private Object attachment(Map<String, Object> args) throws IOException, InterruptedException {
        var item = zotero.json(itemPath(args));
        var data = item.path("data");
        requireType(data, "attachment");
        String mode = data.path("linkMode").asText();
        if (!mode.equals("imported_file") && !mode.equals("imported_url")) {
            throw new IOException("Attachment bytes are available only for Zotero-stored files");
        }
        String type = Documents.type(data.path("contentType").asText(), data.path("filename").asText());
        var context = new LinkedHashMap<String, Object>();
        context.put("source", source(library(args), item));
        context.putAll(Documents.extract(zotero.attachment(itemPath(args) + "/file"), type,
                integer(args, "page", 1, 1, Integer.MAX_VALUE), offset(args), chars(args)));
        return context;
    }

    private Object note(Map<String, Object> args) throws IOException, InterruptedException {
        var item = zotero.json(itemPath(args));
        requireType(item.path("data"), "note");
        var result = Documents.chunk(Jsoup.parse(item.path("data").path("note").asText()).wholeText(),
                offset(args), chars(args));
        result.put("source", source(library(args), item));
        return result;
    }

    private Object indexed(Map<String, Object> args) throws IOException, InterruptedException {
        var index = zotero.json(itemPath(args) + "/fulltext");
        var result = Documents.chunk(index.path("content").asText(), offset(args), chars(args));
        result.put("indexCoverage", Map.of("indexedPages", index.path("indexedPages"), "totalPages",
                index.path("totalPages"), "indexedChars", index.path("indexedChars"), "totalChars",
                index.path("totalChars")));
        result.put("source", source(library(args), zotero.json(itemPath(args))));
        return result;
    }

    private Object duplicates(Map<String, Object> args) throws IOException, InterruptedException {
        int start = start(args);
        int scan = integer(args, "limit", 200, 1, 500);
        int cursor = start;
        int scanned = 0;
        var duplicates = new Duplicates();
        var filters = Map.of("itemType", "-attachment || note", "sort", "dateAdded", "direction", "asc");
        Integer next;
        do {
            var page = zotero.page(library(args) + "/items/top", filters, cursor, Math.min(100, scan - scanned));
            page.items().forEach(duplicates::add);
            scanned += page.items().size();
            next = page.nextStart();
            if (next != null) cursor = next;
        } while (next != null && scanned < scan);
        var result = new LinkedHashMap<String, Object>();
        result.put("matches", duplicates.findings());
        result.put("start", start);
        result.put("scanned", scanned);
        result.put("nextStart", next);
        return result;
    }

    private Object bibtex(Map<String, Object> args) throws IOException, InterruptedException {
        var result = new LinkedHashMap<String, Object>();
        byte[] exported;
        if (args.containsKey("key")) {
            exported = zotero.get(itemPath(args), Map.of("format", "bibtex"));
        } else {
            var path = library(args) + "/items/top";
            var filters = Map.of("itemType", "-attachment || note", "sort", "dateAdded", "direction", "asc");
            var page = zotero.page(path, filters, start(args), integer(args, "limit", 20, 1, 50));
            var keys = new ArrayList<String>();
            page.items().forEach(item -> keys.add(key(item.path("key").asText())));
            exported = keys.isEmpty() ? new byte[0] : zotero.get(library(args) + "/items",
                    Map.of("format", "bibtex", "itemKey", String.join(",", keys), "limit", Integer.toString(keys.size())));
            result.put("nextStart", page.nextStart());
        }
        result.put("bibtex", new String(exported, StandardCharsets.UTF_8));
        return result;
    }

    private Object source(String library, JsonNode item) throws IOException, InterruptedException {
        String parent = item.path("data").path("parentItem").asText();
        var reference = parent.isEmpty() ? item : zotero.json(library + "/items/" + key(parent));
        return Map.of("itemKey", item.path("key").asText(), "itemUrl", sourceUrl(item),
                "referenceKey", reference.path("key").asText(), "referenceUrl", sourceUrl(reference),
                "bibliography", reference.path("data"));
    }

    private static String sourceUrl(JsonNode item) {
        String url = item.path("links").path("alternate").path("href").asText();
        if (url.startsWith("https://www.zotero.org/") || url.startsWith("https://zotero.org/")) {
            return url;
        }
        var library = item.path("library");
        String id = library.path("id").asText();
        String key = item.path("key").asText();
        if (id.matches("[0-9]+") && key.matches("[A-Z0-9]{8}")) {
            String kind = "group".equals(library.path("type").asText()) ? "groups" : "users";
            return "https://www.zotero.org/" + kind + "/" + id + "/items/" + key;
        }
        return "";
    }

    private static String title(JsonNode item) {
        return item.path("data").path("title").asText(item.path("key").asText());
    }

    private static Map<String, Object> page(Zotero.Page page) {
        var result = new LinkedHashMap<String, Object>();
        result.put("items", page.items());
        result.put("nextStart", page.nextStart());
        result.put("total", page.total());
        return result;
    }

    private static void requireType(JsonNode data, String type) {
        if (!type.equals(data.path("itemType").asText())) {
            throw new IllegalArgumentException("Expected a Zotero " + type + " key");
        }
    }

    private String itemPath(Map<String, Object> args) {
        return library(args) + "/items/" + key(required(args, "key"));
    }

    private String library(Map<String, Object> args) {
        return libraries.get(alias(args));
    }

    private String alias(Map<String, Object> args) {
        if (!args.containsKey("library")) {
            if (libraries.containsKey("personal")) return "personal";
            if (libraries.size() == 1) return libraries.keySet().iterator().next();
        }
        String alias = required(args, "library");
        if (!libraries.containsKey(alias)) throw new IllegalArgumentException("Unknown configured library");
        return alias;
    }

    private static String key(String value) {
        if (!value.matches("[A-Z0-9]{8}")) throw new IllegalArgumentException("Invalid Zotero key");
        return value;
    }

    private static String required(Map<String, Object> args, String name) {
        if (!(args.get(name) instanceof String value) || value.isBlank() || value.length() > 512) {
            throw new IllegalArgumentException("Provide a nonempty " + name + " of at most 512 characters");
        }
        return value.strip();
    }

    private static int integer(Map<String, Object> args, String name, int fallback, int min, int max) {
        if (!args.containsKey(name)) return fallback;
        if (!(args.get(name) instanceof Number value) || value.doubleValue() != value.intValue()
                || value.intValue() < min || value.intValue() > max) {
            throw new IllegalArgumentException(name + " must be an integer between " + min + " and " + max);
        }
        return value.intValue();
    }

    private static int start(Map<String, Object> args) { return integer(args, "start", 0, 0, 100_000_000); }
    private static int limit(Map<String, Object> args) { return integer(args, "limit", 20, 1, 100); }
    private static int offset(Map<String, Object> args) { return integer(args, "offset", 0, 0, 5_000_000); }
    private static int chars(Map<String, Object> args) { return integer(args, "chars", 6000, 1, 16_000); }

    private static Map<String, Object> fields(Map<String, Object> first, Map<String, Object> second) {
        var result = new HashMap<>(first);
        result.putAll(second);
        return result;
    }

    private static SyncToolSpecification tool(String name, String description, boolean scoped,
            Map<String, Object> properties, List<String> required, Operation operation) {
        var schema = Map.<String, Object>of("type", "object", "properties",
                scoped ? fields(properties, LIBRARY) : properties, "required", required,
                "additionalProperties", false);
        var tool = Tool.builder(name, schema).description(description).annotations(ToolAnnotations.builder()
                .readOnlyHint(true).destructiveHint(false).idempotentHint(true).openWorldHint(true).build()).build();
        return SyncToolSpecification.builder().tool(tool).callHandler((exchange, request) -> {
            try {
                var args = request.arguments() == null ? Map.<String, Object>of() : request.arguments();
                return result(Zotero.JSON.writeValueAsString(operation.run(args)), false);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
                return result("Request interrupted", true);
            } catch (IllegalArgumentException | Zotero.StatusException | Zotero.RateLimitException expected) {
                return result(expected.getMessage(), true);
            } catch (IOException | RuntimeException failure) {
                return result("Unable to read or process the requested Zotero content", true);
            }
        }).build();
    }

    private static CallToolResult result(String text, boolean error) {
        return CallToolResult.builder().content(List.of(TextContent.builder(text).build())).isError(error).build();
    }

    @FunctionalInterface
    private interface Operation {
        Object run(Map<String, Object> args) throws IOException, InterruptedException;
    }
}
