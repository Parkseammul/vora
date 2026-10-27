import { createReadStream } from "node:fs";
import { fileURLToPath } from "node:url";

const referenceFile = fileURLToPath(new URL("../home.png", import.meta.url));

export default function localDesignReference() {
  return {
    name: "local-design-reference",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use("/__prototype__/home.png", (_request, response) => {
        const image = createReadStream(referenceFile);
        image.on("error", () => {
          if (!response.headersSent) {
            response.statusCode = 404;
            response.end();
          } else {
            response.destroy();
          }
        });
        response.setHeader("Content-Type", "image/png");
        response.setHeader("Cache-Control", "no-store");
        image.pipe(response);
      });
    },
  };
}
