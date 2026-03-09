/**
 * Cloudflare Worker: GitHub Actions Trigger
 *
 * Umgebungsvariablen (in Cloudflare Dashboard unter Settings > Variables setzen):
 *   GH_TOKEN  – GitHub Personal Access Token (scope: actions:write)
 *
 * Hardcodiert:
 *   GH_OWNER  – veit-ba
 *   GH_REPO   – saisonmanager-report
 */

const GH_OWNER = "veit-ba";
const GH_REPO  = "saisonmanager-report";
const WORKFLOW  = "update-report.yml";

const CORS = {
  "Access-Control-Allow-Origin":  "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: CORS });
    }

    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    const apiUrl =
      `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}` +
      `/actions/workflows/${WORKFLOW}/dispatches`;

    const res = await fetch(apiUrl, {
      method: "POST",
      headers: {
        Authorization:  `Bearer ${env.GH_TOKEN}`,
        Accept:         "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent":   "Cloudflare-Worker",
      },
      body: JSON.stringify({ ref: "main" }),
    });

    if (res.status === 204) {
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { ...CORS, "Content-Type": "application/json" },
      });
    }

    const body = await res.text();
    return new Response(JSON.stringify({ ok: false, error: body }), {
      status: res.status,
      headers: { ...CORS, "Content-Type": "application/json" },
    });
  },
};
