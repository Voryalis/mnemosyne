package io.mnemosyne;

import io.modelcontextprotocol.json.McpJsonDefaults;
import io.modelcontextprotocol.server.McpServer;
import io.modelcontextprotocol.server.transport.StdioServerTransportProvider;
import io.modelcontextprotocol.spec.McpSchema.ServerCapabilities;

import java.time.Duration;

public final class Main {
    private Main() {}

    public static void main(String[] args) throws Exception {
        if (args.length > 1 || (args.length == 1 && !args[0].equals("stdio") && !args[0].equals("http"))) {
            System.err.println("Usage: java -jar mnemosyne.jar [stdio|http]");
            System.exit(2);
        }
        var config = new Config(System.getenv());
        var tools = new ResearchTools(new Zotero(config), config.libraries);
        if (args.length == 1 && args[0].equals("http")) {
            HttpEndpoint.run(config, tools);
        } else {
            var transport = new StdioServerTransportProvider(McpJsonDefaults.getMapper());
            McpServer.sync(transport).serverInfo("MNEMOSYNE", "0.1.0")
                    .capabilities(ServerCapabilities.builder().tools(false).build())
                    .requestTimeout(Duration.ofMinutes(3)).tools(tools.specifications()).build();
        }
    }
}
