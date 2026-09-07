const fs = require("node:fs");
const fsp = require("node:fs/promises");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const MIME_TYPES = {
  ".css": "text/css; charset=utf-8",
  ".gif": "image/gif",
  ".html": "text/html; charset=utf-8",
  ".ico": "image/x-icon",
  ".jpg": "image/jpeg",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".txt": "text/plain; charset=utf-8",
  ".webp": "image/webp",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

function resolveProtocolPath(distRoot, pathname, { existsSync = fs.existsSync } = {}) {
  const normalizedRoot = path.resolve(distRoot);
  const decodedPath = decodeURIComponent(pathname || "/").replace(/^[/\\]+/, "");
  const candidate = path.resolve(normalizedRoot, decodedPath || "index.html");
  const relative = path.relative(normalizedRoot, candidate);
  const escapesRoot = relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative);

  if (!escapesRoot && existsSync(candidate)) {
    return candidate;
  }
  return path.join(normalizedRoot, "index.html");
}

function contentTypeFor(filePath) {
  return MIME_TYPES[path.extname(filePath).toLowerCase()] || "application/octet-stream";
}

function registerAppProtocol(protocol, distRoot) {
  protocol.handle("aegis", async (request) => {
    const requestUrl = new URL(request.url);
    const filePath = resolveProtocolPath(distRoot, requestUrl.pathname);
    const body = await fsp.readFile(filePath);
    return new Response(body, {
      headers: {
        "Content-Type": contentTypeFor(filePath),
      },
    });
  });
}

function fileUrlForProtocolPath(distRoot, pathname) {
  return pathToFileURL(resolveProtocolPath(distRoot, pathname)).toString();
}

module.exports = {
  contentTypeFor,
  fileUrlForProtocolPath,
  registerAppProtocol,
  resolveProtocolPath,
};
