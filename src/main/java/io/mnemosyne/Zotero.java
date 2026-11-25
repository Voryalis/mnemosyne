package io.mnemosyne;

import okhttp3.HttpUrl;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

import java.io.IOException;
import java.net.Proxy;
import java.time.Duration;
import java.time.Instant;
import java.time.ZonedDateTime;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.Map;
import java.util.Objects;

final class Zotero {
    static final JsonMapper JSON = new JsonMapper();
    private static final int MAX_JSON_BYTES = 16 * 1024 * 1024;
    private static final int MAX_FILE_BYTES = 25 * 1024 * 1024;
    private final OkHttpClient http;
    private final HttpUrl origin;
    private final String key;
    private Instant nextRequest = Instant.EPOCH;

    Zotero(Config config) {
        this(HttpUrl.get("https://api.zotero.org"), config.zoteroKey, config.proxy);
    }

    Zotero(HttpUrl origin, String key, Proxy proxy) {
        this(origin, key, new OkHttpClient.Builder().proxy(proxy));
    }

    Zotero(HttpUrl origin, String key, OkHttpClient.Builder client) {
        this.origin = origin;
        this.key = key;
        http = client.followRedirects(false)
                .followSslRedirects(false).retryOnConnectionFailure(false)
                .connectTimeout(Duration.ofSeconds(8)).callTimeout(Duration.ofSeconds(25)).build();
    }

    record Page(JsonNode items, Integer nextStart, Integer total) {}

    Page page(String path, Map<String, String> query, int start, int limit) throws IOException, InterruptedException {
        var url = url(path, query).addQueryParameter("start", Integer.toString(start))
                .addQueryParameter("limit", Integer.toString(limit)).build();
        try (var response = request(url, true, false)) {
            var items = JSON.readTree(read(response, MAX_JSON_BYTES));
            if (!items.isArray()) {
                throw new IOException("Zotero returned an invalid list");
            }
            String count = response.header("Total-Results");
            Integer total = count != null && count.matches("[0-9]{1,9}") ? Integer.valueOf(count) : null;
            boolean more = total != null ? (long) start + items.size() < total
                    : response.headers("Link").stream().anyMatch(link -> link.contains("rel=\"next\""));
            return new Page(items, more && !items.isEmpty() ? start + items.size() : null, total);
        }
    }

    JsonNode json(String path) throws IOException, InterruptedException {
        return JSON.readTree(get(path, Map.of()));
    }

    byte[] get(String path, Map<String, String> query) throws IOException, InterruptedException {
        try (var response = request(url(path, query).build(), true, false)) {
            return read(response, MAX_JSON_BYTES);
        }
    }

    byte[] attachment(String path) throws IOException, InterruptedException {
        HttpUrl target = url(path, Map.of()).build();
        for (int hop = 0; hop < 5; hop++) {
            try (var response = request(target, hop == 0, true)) {
                if (response.isSuccessful()) {
                    return read(response, MAX_FILE_BYTES);
                }
                String location = response.header("Location");
                HttpUrl redirect = location == null ? null : target.resolve(location);
                if (redirect == null || !redirect.isHttps() || redirect.port() != 443
                        || !redirect.username().isEmpty() || !redirect.password().isEmpty()) {
                    throw new IOException("Attachment storage returned an invalid HTTPS redirect");
                }
                target = redirect;
            }
        }
        throw new IOException("Too many attachment storage redirects");
    }

    private HttpUrl.Builder url(String path, Map<String, String> query) {
        var builder = origin.newBuilder().encodedPath(path);
        query.forEach(builder::addQueryParameter);
        return builder;
    }

    private Response request(HttpUrl url, boolean api, boolean allowRedirect) throws IOException, InterruptedException {
        var builder = new Request.Builder().url(url).get();
        if (api) {
            builder.header("Zotero-API-Version", "3");
            if (!key.isEmpty()) {
                builder.header("Zotero-API-Key", key);
            }
        }
        for (int attempt = 0; attempt < 3; attempt++) {
            Response response;
            try {
                response = api ? apiRequest(builder.build(), attempt) : http.newCall(builder.build()).execute();
            } catch (IOException failure) {
                if (failure instanceof RateLimitException) {
                    throw failure;
                }
                if (attempt == 2 || failure instanceof javax.net.ssl.SSLException) {
                    throw new IOException("Connection failed while contacting Zotero or attachment storage");
                }
                Thread.sleep(500L << attempt);
                continue;
            }
            int status = response.code();
            if (response.isSuccessful() || (allowRedirect && switch (status) {
                case 301, 302, 303, 307, 308 -> true;
                default -> false;
            })) {
                return response;
            }
            response.close();
            if (attempt == 2 || !retryable(status)) {
                throw new StatusException(status);
            }
            if (!api) {
                long delay = Math.max(1L << attempt, delaySeconds(response.header("Retry-After")));
                if (delay > 8) {
                    throw new StatusException(status);
                }
                Thread.sleep(delay * 1000);
            }
        }
        throw new IllegalStateException("Retry loop exhausted");
    }

    private synchronized Response apiRequest(Request request, int attempt) throws IOException, InterruptedException {
        long wait = Duration.between(Instant.now(), nextRequest).toMillis();
        if (wait > 8000) {
            throw new RateLimitException();
        }
        if (wait > 0) {
            Thread.sleep(wait + 1);
        }
        var response = http.newCall(request).execute();
        long delay = Math.max(delaySeconds(response.header("Backoff")), delaySeconds(response.header("Retry-After")));
        if (retryable(response.code())) {
            delay = Math.max(delay, 1L << attempt);
        }
        nextRequest = Instant.now().plusSeconds(delay);
        return response;
    }

    private static boolean retryable(int status) {
        return status == 429 || status == 500 || status == 502 || status == 503 || status == 504;
    }

    static long delaySeconds(String header) {
        if (header == null) {
            return 0;
        }
        try {
            return Math.clamp(Long.parseLong(header.strip()), 0, Integer.MAX_VALUE);
        } catch (NumberFormatException ignored) {
            try {
                var seconds = Duration.between(Instant.now(), ZonedDateTime.parse(header,
                        DateTimeFormatter.RFC_1123_DATE_TIME).toInstant());
                return Math.clamp(seconds.isNegative() ? 0 : seconds.toSeconds() + 1, 0, Integer.MAX_VALUE);
            } catch (DateTimeParseException invalid) {
                return 0;
            }
        }
    }

    private static byte[] read(Response response, int limit) throws IOException {
        var body = Objects.requireNonNull(response.body());
        if (body.contentLength() > limit) {
            throw new IOException("Response exceeds the configured size limit");
        }
        byte[] bytes = body.byteStream().readNBytes(limit + 1);
        if (bytes.length > limit) {
            throw new IOException("Response exceeds the configured size limit");
        }
        return bytes;
    }

    static final class StatusException extends IOException {
        final int status;

        StatusException(int status) {
            super("Zotero or attachment storage returned HTTP " + status);
            this.status = status;
        }
    }

    static final class RateLimitException extends IOException {
        RateLimitException() {
            super("Zotero requested a backoff. Try again later");
        }
    }
}
