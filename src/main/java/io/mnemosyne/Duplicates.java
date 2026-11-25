package io.mnemosyne;

import okhttp3.HttpUrl;
import tools.jackson.databind.JsonNode;

import java.text.Normalizer;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

final class Duplicates {
    private final Map<String, List<String>> matches = new LinkedHashMap<>();

    void add(JsonNode item) {
        var data = item.path("data");
        String key = item.path("key").asText();
        add("DOI", data.path("DOI").asText().strip().toLowerCase(Locale.ROOT)
                .replaceFirst("^(https?://(?:dx\\.)?doi\\.org/|doi:\\s*)", ""), key);
        HttpUrl url = HttpUrl.parse(data.path("url").asText().strip());
        if (url != null) {
            add("URL", url.newBuilder().fragment(null).build().toString(), key);
        }
        add("title", Normalizer.normalize(data.path("title").asText(), Normalizer.Form.NFKC)
                .toLowerCase(Locale.ROOT).replaceAll("[^\\p{L}\\p{N}\\p{M}]+", " ").strip(), key);
    }

    private void add(String field, String normalized, String key) {
        if (!normalized.isEmpty() && !key.isEmpty()) {
            matches.computeIfAbsent(field + "=" + normalized, ignored -> new ArrayList<>()).add(key);
        }
    }

    List<Map<String, Object>> findings() {
        return matches.entrySet().stream().filter(entry -> entry.getValue().size() > 1)
                .map(entry -> {
                    String[] identifier = entry.getKey().split("=", 2);
                    return Map.<String, Object>of("field", identifier[0], "value", identifier[1],
                            "keys", entry.getValue());
                }).toList();
    }
}
