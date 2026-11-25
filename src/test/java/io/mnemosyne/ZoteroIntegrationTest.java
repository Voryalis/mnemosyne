package io.mnemosyne;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import io.modelcontextprotocol.spec.McpSchema.CallToolRequest;
import io.modelcontextprotocol.spec.McpSchema.TextContent;
import okhttp3.HttpUrl;
import okhttp3.OkHttpClient;
import okhttp3.Protocol;
import okhttp3.Response;
import okhttp3.ResponseBody;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.Proxy;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static org.junit.jupiter.api.Assertions.*;

class ZoteroIntegrationTest {
    private static final String BOOK = """
            {"key":"TESTBOOK","library":{"type":"group","id":42},"data":
            {"itemType":"book","title":"Memory & Citations","DOI":"10.1000/demo","creators":[{"lastName":"Author"}]}}
            """;
    private HttpServer server;
    private HttpUrl base;

    @BeforeEach
    void start() throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.start();
        base = HttpUrl.get("http://127.0.0.1:" + server.getAddress().getPort());
    }

    @AfterEach
    void stop() {
        server.stop(0);
    }

    @Test
    void privateGroupSearchAndCitationFetchUseTheSameStableId() {
        var observed = new AtomicReference<String>();
        server.createContext("/groups/42/items/top", request -> {
            observed.set(request.getRequestHeaders().getFirst("Zotero-API-Key") + " "
                    + request.getRequestHeaders().getFirst("Zotero-API-Version") + " " + request.getRequestURI());
            request.getResponseHeaders().set("Total-Results", "3");
            respond(request, 200, "[" + BOOK + "]");
        });
        server.createContext("/groups/42/items/TESTBOOK", request -> respond(request, 200, BOOK));
        var tools = new ResearchTools(new Zotero(base, "mock-key", Proxy.NO_PROXY),
                Map.of("group:42", "/groups/42"));

        var search = Zotero.JSON.readTree(call(tools, "search", Map.of("query", "a & b", "start", 1, "limit", 1)));
        assertEquals("mock-key 3 /groups/42/items/top", observed.get().split("\\?")[0]);
        assertTrue(observed.get().contains("q=a%20%26%20b"));
        assertEquals(2, search.path("nextStart").asInt());
        String id = search.path("results").get(0).path("id").asText();
        var fetch = Zotero.JSON.readTree(call(tools, "fetch", Map.of("id", id)));
        assertEquals("group:42/TESTBOOK", id);
        assertEquals("https://www.zotero.org/groups/42/items/TESTBOOK", fetch.path("url").asText());
        assertEquals("10.1000/demo", fetch.path("metadata").path("bibliography").path("DOI").asText());
    }

    @Test
    void stripsCredentialsBeforeStorageAndNeverFollowsInsecureRedirects() throws Exception {
        server.createContext("/file", request -> {
            if (!"mock-key".equals(request.getRequestHeaders().getFirst("Zotero-API-Key"))) {
                respond(request, 403, "Missing API key");
                return;
            }
            request.getResponseHeaders().set("Location", "https://storage.example.test/object?signature=mock");
            respond(request, 302, "");
        });
        server.createContext("/insecure", request -> {
            request.getResponseHeaders().set("Location", "http://storage.example.test/object");
            respond(request, 302, "");
        });
        var storageCalls = new AtomicInteger();
        var client = new OkHttpClient.Builder().proxy(Proxy.NO_PROXY).addInterceptor(chain -> {
            var request = chain.request();
            if (!request.url().host().equals("storage.example.test")) {
                return chain.proceed(request);
            }
            storageCalls.incrementAndGet();
            assertNull(request.header("Zotero-API-Key"));
            assertNull(request.header("Zotero-API-Version"));
            assertNull(request.header("Authorization"));
            assertEquals("mock", request.url().queryParameter("signature"));
            return new Response.Builder().request(request).protocol(Protocol.HTTP_1_1)
                    .code(200).message("OK").body(ResponseBody.create("document", null)).build();
        });
        var zotero = new Zotero(base, "mock-key", client);
        assertEquals("document", new String(zotero.attachment("/file"), StandardCharsets.UTF_8));
        assertThrows(IOException.class, () -> zotero.attachment("/insecure"));
        assertEquals(1, storageCalls.get());
    }

    @Test
    void retries429AndHonorsSuccessfulBackoffForFollowingCalls() throws Exception {
        var requests = new AtomicInteger();
        server.createContext("/limited", request -> {
            if (requests.incrementAndGet() == 1) {
                request.getResponseHeaders().set("Retry-After", "1");
                respond(request, 429, "");
            } else {
                request.getResponseHeaders().set("Backoff", "60");
                respond(request, 200, "{}");
            }
        });
        var zotero = new Zotero(base, "", Proxy.NO_PROXY);
        long start = System.nanoTime();
        assertTrue(zotero.json("/limited").isObject());
        assertTrue(System.nanoTime() - start >= 1_000_000_000L);
        assertThrows(Zotero.RateLimitException.class, () -> zotero.json("/limited"));
        assertEquals(2, requests.get());
    }

    @Test
    void notesAndIndexedTextRetainParentCitationAndChunkOffsets() {
        server.createContext("/users/42/items/TESTBOOK", request -> respond(request, 200, BOOK));
        server.createContext("/users/42/items/TESTNOTE", request -> respond(request, 200, """
                {"key":"TESTNOTE","data":{"itemType":"note","parentItem":"TESTBOOK","note":"<p>A &amp; B</p>"}}
                """));
        server.createContext("/users/42/items/TESTFILE", request -> respond(request, 200, """
                {"key":"TESTFILE","data":{"itemType":"attachment","parentItem":"TESTBOOK"}}
                """));
        server.createContext("/users/42/items/TESTFILE/fulltext", request -> respond(request, 200,
                "{\"content\":\"Indexed text\",\"indexedPages\":2,\"totalPages\":3}"));
        var tools = new ResearchTools(new Zotero(base, "mock-key", Proxy.NO_PROXY), Map.of("personal", "/users/42"));
        var note = Zotero.JSON.readTree(call(tools, "read_note", Map.of("key", "TESTNOTE", "chars", 3)));
        assertEquals("A &", note.path("text").asText());
        assertEquals(3, note.path("nextOffset").asInt());
        assertEquals("TESTBOOK", note.path("source").path("referenceKey").asText());
        var index = Zotero.JSON.readTree(call(tools, "read_indexed_text", Map.of("key", "TESTFILE")));
        assertEquals("Indexed text", index.path("text").asText());
        assertEquals(2, index.path("indexCoverage").path("indexedPages").asInt());
    }

    @Test
    void fullTextUsesZoteroIndexAndBibtexExportsOnlyTheRequestedPage() {
        var searchQuery = new AtomicReference<String>();
        var bibtexQuery = new AtomicReference<String>();
        server.createContext("/users/42/items/top", request -> {
            request.getResponseHeaders().set("Total-Results", "12");
            respond(request, 200, "[" + BOOK + "]");
        });
        server.createContext("/users/42/items", request -> {
            String query = request.getRequestURI().getRawQuery();
            if (query.contains("format=bibtex")) {
                bibtexQuery.set(query);
                respond(request, 200, "@book{example, title={Memory}}");
            } else {
                searchQuery.set(query);
                respond(request, 200, "[" + BOOK + "]");
            }
        });
        var tools = new ResearchTools(new Zotero(base, "", Proxy.NO_PROXY), Map.of("personal", "/users/42"));
        var indexed = Zotero.JSON.readTree(call(tools, "search_full_text", Map.of("query", "page text")));
        assertEquals("TESTBOOK", indexed.path("items").get(0).path("key").asText());
        assertTrue(searchQuery.get().contains("qmode=everything"));
        var bibtex = Zotero.JSON.readTree(call(tools, "export_bibtex", Map.of("start", 4, "limit", 1)));
        assertEquals("@book{example, title={Memory}}", bibtex.path("bibtex").asText());
        assertEquals(5, bibtex.path("nextStart").asInt());
        assertTrue(bibtexQuery.get().contains("itemKey=TESTBOOK"));
    }

    private static String call(ResearchTools tools, String name, Map<String, Object> args) {
        var spec = tools.specifications().stream().filter(tool -> tool.tool().name().equals(name)).findFirst().orElseThrow();
        var result = spec.callHandler().apply(null, new CallToolRequest(name, args));
        String text = ((TextContent) result.content().getFirst()).text();
        assertFalse(result.isError(), text);
        return text;
    }

    private static void respond(HttpExchange exchange, int status, String text) throws IOException {
        byte[] body = text.getBytes(StandardCharsets.UTF_8);
        try (exchange) {
            exchange.sendResponseHeaders(status, body.length == 0 ? -1 : body.length);
            exchange.getResponseBody().write(body);
        }
    }
}
