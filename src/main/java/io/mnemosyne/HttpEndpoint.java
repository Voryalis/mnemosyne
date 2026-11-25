package io.mnemosyne;

import io.modelcontextprotocol.server.McpServer;
import io.modelcontextprotocol.server.transport.DefaultServerTransportSecurityValidator;
import io.modelcontextprotocol.server.transport.HttpServletStreamableServerTransportProvider;
import io.modelcontextprotocol.spec.McpSchema.ServerCapabilities;
import jakarta.servlet.DispatcherType;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.apache.catalina.startup.Tomcat;
import org.apache.tomcat.util.descriptor.web.FilterDef;
import org.apache.tomcat.util.descriptor.web.FilterMap;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;

final class HttpEndpoint {
    private HttpEndpoint() {}

    static void run(Config config, ResearchTools tools) throws Exception {
        if (config.httpToken.isEmpty()) {
            throw new IllegalArgumentException("Set MNEMOSYNE_HTTP_TOKEN to start the HTTP transport");
        }
        var transport = HttpServletStreamableServerTransportProvider.builder().mcpEndpoint("/mcp")
                .keepAliveInterval(Duration.ofSeconds(20))
                .securityValidator(DefaultServerTransportSecurityValidator.builder()
                        .allowedHost("localhost:*").allowedHost("127.0.0.1:*").build())
                .build();
        var server = McpServer.sync(transport).serverInfo("MNEMOSYNE", "0.1.0")
                .capabilities(ServerCapabilities.builder().tools(false).build())
                .requestTimeout(Duration.ofMinutes(3)).tools(tools.specifications()).build();
        var tomcat = new Tomcat();
        tomcat.setPort(config.port);
        tomcat.setBaseDir(System.getProperty("java.io.tmpdir"));
        var connector = tomcat.getConnector();
        connector.setProperty("address", "127.0.0.1");
        connector.setAsyncTimeout(Duration.ofMinutes(4).toMillis());
        var context = tomcat.addContext("", System.getProperty("java.io.tmpdir"));
        var servlet = Tomcat.addServlet(context, "mcp", transport);
        servlet.setAsyncSupported(true);
        servlet.setLoadOnStartup(1);
        context.addServletMappingDecoded("/mcp", "mcp");
        addAuthentication(context, config.httpToken);
        tomcat.start();
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            server.closeGracefully();
            try {
                tomcat.stop();
                tomcat.destroy();
            } catch (Exception failure) {
                System.err.println("MCP HTTP shutdown did not complete cleanly");
            }
        }));
        tomcat.getServer().await();
    }

    private static void addAuthentication(org.apache.catalina.Context context, String token) {
        byte[] expected = ("Bearer " + token).getBytes(StandardCharsets.US_ASCII);
        var auth = new FilterDef();
        auth.setFilterName("bearer");
        auth.setAsyncSupported("true");
        auth.setFilter((request, response, chain) -> {
            var http = (HttpServletRequest) request;
            var reply = (HttpServletResponse) response;
            if (!authorized(expected, http.getHeader("Authorization"))) {
                reply.setHeader("WWW-Authenticate", "Bearer");
                reply.setStatus(401);
                return;
            }
            chain.doFilter(request, response);
        });
        context.addFilterDef(auth);
        var mapping = new FilterMap();
        mapping.setFilterName("bearer");
        mapping.setDispatcher(DispatcherType.REQUEST.name());
        mapping.addURLPattern("/mcp");
        context.addFilterMap(mapping);
    }

    static boolean authorized(byte[] expected, String actual) {
        return actual != null && MessageDigest.isEqual(expected, actual.getBytes(StandardCharsets.US_ASCII));
    }
}
