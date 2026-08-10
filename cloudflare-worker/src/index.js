






const DEFAULT_ORIGIN = "https://sefbot-production.up.railway.app";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    
    if (path !== "/sefbot" && !path.startsWith("/sefbot/")) {
      return new Response("Not found", { status: 404 });
    }

    const origin = (env.SEFBOT_ORIGIN || DEFAULT_ORIGIN).replace(/\/$/, "");
    const target = new URL(origin + path + url.search);

    const headers = new Headers(request.headers);
    headers.set("Host", new URL(origin).host);
    headers.delete("cf-connecting-ip"); 

    const clientIp =
      request.headers.get("CF-Connecting-IP") ||
      (request.headers.get("X-Forwarded-For") || "").split(",")[0].trim() ||
      "";
    if (clientIp) {
      headers.set("X-Forwarded-For", clientIp);
      headers.set("X-Real-IP", clientIp);
    }
    headers.set("X-Forwarded-Proto", "https");
    headers.set("X-Forwarded-Host", url.host);

    const init = {
      method: request.method,
      headers,
      redirect: "manual",
    };
    if (request.method !== "GET" && request.method !== "HEAD") {
      init.body = request.body;
    }

    try {
      const upstream = await fetch(target.toString(), init);
      
      const outHeaders = new Headers(upstream.headers);
      outHeaders.delete("transfer-encoding");
      outHeaders.set("X-SefBot-Proxy", "kozzyx.org");
      return new Response(upstream.body, {
        status: upstream.status,
        statusText: upstream.statusText,
        headers: outHeaders,
      });
    } catch (err) {
      return new Response(
        "SefBot page temporarily unavailable. Try again in a moment.",
        { status: 502, headers: { "content-type": "text/plain; charset=utf-8" } },
      );
    }
  },
};
