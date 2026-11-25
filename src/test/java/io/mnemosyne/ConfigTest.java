package io.mnemosyne;

import java.net.InetSocketAddress;
import java.net.Proxy;
import java.nio.charset.StandardCharsets;
import java.util.Map;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

class ConfigTest {
    @Test
    void allowlistsLibrariesAndConfiguresSocksRemoteDns() {
        var config = new Config(Map.of("ZOTERO_USER_ID", "123", "ZOTERO_GROUP_IDS", "42, 43",
                "MNEMOSYNE_PROXY", "socks5h://proxy.example.test:1080"));
        assertEquals(Map.of("personal", "/users/123", "group:42", "/groups/42", "group:43", "/groups/43"),
                config.libraries);
        assertEquals(Proxy.Type.SOCKS, config.proxy.type());
        assertTrue(((InetSocketAddress) config.proxy.address()).isUnresolved());
    }

    @Test
    void acceptsHttpProxyAndRejectsUnsafeConfig() {
        assertEquals(Proxy.Type.HTTP, new Config(Map.of("ZOTERO_GROUP_IDS", "42",
                "MNEMOSYNE_PROXY", "http://proxy.example.test:3128")).proxy.type());
        assertThrows(IllegalArgumentException.class, () -> new Config(Map.of("ZOTERO_GROUP_IDS", "../42")));
        assertThrows(IllegalArgumentException.class, () -> new Config(Map.of("ZOTERO_GROUP_IDS", "42",
                "MNEMOSYNE_PROXY", "http://user:secret@proxy.example.test:3128")));
        assertThrows(IllegalArgumentException.class, () -> new Config(Map.of("ZOTERO_GROUP_IDS", "42",
                "MNEMOSYNE_HTTP_TOKEN", "short")));
    }

    @Test
    void bearerMustMatch() {
        byte[] expected = "Bearer test-value".getBytes(StandardCharsets.US_ASCII);
        assertTrue(HttpEndpoint.authorized(expected, "Bearer test-value"));
        assertFalse(HttpEndpoint.authorized(expected, "Bearer other-value"));
        assertFalse(HttpEndpoint.authorized(expected, null));
    }

    @Test
    void duplicatesMatchNormalizedIdentifiers() {
        var detector = new Duplicates();
        detector.add(Zotero.JSON.readTree("""
                {"key":"ITEM0001","data":{"title":"Memory—Studies","DOI":"https://doi.org/10.1234/X",
                "url":"https://example.org/p#section"}}
                """));
        detector.add(Zotero.JSON.readTree("""
                {"key":"ITEM0002","data":{"title":"MEMORY studies","DOI":"doi:10.1234/x",
                "url":"https://EXAMPLE.org/p"}}
                """));
        assertEquals(3, detector.findings().size());
    }
}
