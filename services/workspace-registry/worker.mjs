const headers = { "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff" };
const empty = status => new Response(null, { status, headers });
const json = value => Response.json(value, { headers });

export default {
  async fetch(request, env) {
    const token = /^Bearer ([0-9a-f]{64})$/.exec(request.headers.get("Authorization") || "");
    if (!token) return empty(401);
    try {
      const buffer = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token[1]));
      const hash = Array.from(new Uint8Array(buffer), x => x.toString(16).padStart(2, "0")).join("");
      const db = env.DB;
      const reader = await db.prepare("SELECT workspace FROM readers WHERE token_sha256=?").bind(hash).first();
      if (!reader) return empty(401);
      if (request.method !== "GET") return empty(405);
      const url = new URL(request.url);
      if (url.search) return empty(404);
      if (url.pathname === "/v1/me") return json(reader);
      const [, version, workspace, route, digest, part, ...extra] = url.pathname.split("/");
      if (version !== "v1" || extra.length) return empty(404);
      if (reader.workspace !== workspace) return empty(403);
      if (route === "current" && digest === undefined) {
        const pointer = await db.prepare("SELECT generation,digest FROM promotions WHERE workspace=? ORDER BY generation DESC LIMIT 1").bind(workspace).first();
        return pointer ? json(pointer) : empty(404);
      }
      if (route !== "releases" || !/^[0-9a-f]{64}$/.test(digest || "")) return empty(404);
      // Staged bytes stay inaccessible until the operator promotes the release once.
      const released = await db.prepare(
        "SELECT parts,size FROM releases WHERE workspace=?1 AND digest=?2 AND EXISTS (SELECT 1 FROM promotions WHERE workspace=?1 AND digest=?2)"
      ).bind(workspace, digest).first();
      if (!released) return empty(404);
      if (part === undefined) return json(released);
      if (!/^(0|[1-9][0-9]{0,3})$/.test(part) || Number(part) >= released.parts) return empty(404);
      const row = await db.prepare("SELECT body FROM chunks WHERE workspace=? AND digest=? AND part=?").bind(workspace, digest, Number(part)).first();
      return row ? new Response(new Uint8Array(row.body), { headers }) : empty(503);
    } catch {
      return empty(503);
    }
  }
};
