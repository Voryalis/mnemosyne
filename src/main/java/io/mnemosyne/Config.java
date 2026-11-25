package io.mnemosyne;

import java.net.InetSocketAddress;
import java.net.Proxy;
import java.net.URI;
import java.util.LinkedHashMap;
import java.util.Map;

final class Config {
    final Map<String, String> libraries;
    final String zoteroKey;
    final Proxy proxy;
    final String httpToken;
    final int port;

    Config(Map<String, String> env) {
        zoteroKey = env.getOrDefault("ZOTERO_API_KEY", "");
        httpToken = env.getOrDefault("MNEMOSYNE_HTTP_TOKEN", "");
        if (!httpToken.isEmpty() && !httpToken.matches("[A-Za-z0-9._~-]{32,256}")) {
            throw new IllegalArgumentException("Configure an HTTP token with 32–256 URL-safe characters");
        }
        port = Integer.parseInt(env.getOrDefault("MNEMOSYNE_PORT", "8080"));
        if (port < 1 || port > 65535) {
            throw new IllegalArgumentException("Invalid MCP port");
        }
        proxy = proxy(env.getOrDefault("MNEMOSYNE_PROXY", ""));
        var configured = new LinkedHashMap<String, String>();
        String user = env.getOrDefault("ZOTERO_USER_ID", "").strip();
        if (!user.isEmpty()) {
            configured.put("personal", "/users/" + numericId(user));
        }
        String groups = env.getOrDefault("ZOTERO_GROUP_IDS", "");
        if (!groups.isBlank()) {
            for (String entry : groups.split(",", -1)) {
                String group = numericId(entry.strip());
                configured.put("group:" + group, "/groups/" + group);
            }
        }
        if (configured.isEmpty()) {
            throw new IllegalArgumentException("Configure ZOTERO_USER_ID or ZOTERO_GROUP_IDS");
        }
        libraries = Map.copyOf(configured);
    }

    private static String numericId(String value) {
        if (!value.matches("[1-9][0-9]{0,19}")) {
            throw new IllegalArgumentException("Zotero library IDs must be positive decimal integers");
        }
        return value;
    }

    private static Proxy proxy(String value) {
        if (value.isBlank()) {
            return Proxy.NO_PROXY;
        }
        URI address = URI.create(value);
        if (address.getHost() == null || address.getPort() < 1 || address.getPort() > 65535
                || address.getRawUserInfo() != null || !address.getRawPath().isEmpty()
                || address.getRawQuery() != null || address.getRawFragment() != null) {
            throw new IllegalArgumentException("Proxy must be a scheme, host and port");
        }
        var type = switch (address.getScheme()) {
            case "http" -> Proxy.Type.HTTP;
            case "socks5", "socks5h" -> Proxy.Type.SOCKS;
            default -> throw new IllegalArgumentException("Unsupported proxy scheme");
        };
        return new Proxy(type, InetSocketAddress.createUnresolved(address.getHost(), address.getPort()));
    }
}
