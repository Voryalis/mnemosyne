package io.mnemosyne;

import okhttp3.HttpUrl;
import org.junit.jupiter.api.Test;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.concurrent.Executors;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class ProxyIntegrationTest {
    @Test
    void socks5SendsTheUnresolvedDestinationToTheProxy() throws Exception {
        String destination = "zotero-test.invalid";
        try (var proxy = new ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"));
             var worker = Executors.newSingleThreadExecutor()) {
            proxy.setSoTimeout(5000);
            var request = worker.submit(() -> {
                try (var client = proxy.accept()) {
                    client.setSoTimeout(5000);
                    var in = client.getInputStream();
                    var out = client.getOutputStream();
                    assertEquals(5, in.read());
                    int methods = in.read();
                    in.readNBytes(methods);
                    out.write(new byte[]{5, 0});
                    out.flush();
                    assertArrayEquals(new byte[]{5, 1, 0, 3}, in.readNBytes(4));
                    int length = in.read();
                    assertEquals(destination, new String(in.readNBytes(length), StandardCharsets.US_ASCII));
                    assertArrayEquals(new byte[]{0, 80}, in.readNBytes(2));
                    out.write(new byte[]{5, 0, 0, 1, 127, 0, 0, 1, 0, 80});
                    out.flush();
                    var reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.US_ASCII));
                    String start = reader.readLine();
                    for (String header; (header = reader.readLine()) != null && !header.isEmpty();) {
                        // Consume request headers before sending the mocked Zotero response.
                    }
                    out.write(("HTTP/1.1 200 OK\r\nConnection: close\r\n"
                            + "Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
                            .getBytes(StandardCharsets.US_ASCII));
                    out.flush();
                    return start;
                }
            });
            var config = new Config(Map.of("ZOTERO_USER_ID", "42", "MNEMOSYNE_PROXY",
                    "socks5h://127.0.0.1:" + proxy.getLocalPort()));
            var zotero = new Zotero(HttpUrl.get("http://" + destination), "", config.proxy);
            assertTrue(zotero.json("/probe").isObject());
            assertEquals("GET /probe HTTP/1.1", request.get());
        }
    }
}
