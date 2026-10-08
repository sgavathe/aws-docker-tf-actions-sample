// Renders the repo's Markdown docs into static, crawlable HTML pages under public/docs/,
// and writes public/sitemap.xml. Runs automatically before `npm run build` (prebuild).
//
//   README.md            -> /docs/index.html  (paths ending in / are rewritten to the app by CloudFront)
//   docs/grid-cascade.md -> /docs/grid-cascade.html
//   docs/hosting.md      -> /docs/hosting.html
//   SECURITY.md          -> /docs/security.html
//
// Links between these pages become .html links; links to any other repo file go to GitHub.
// ```mermaid blocks are drawn in the browser by Mermaid (loaded from a CDN).
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, normalize, posix, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Marked } from "marked";

const SITE = "https://map.spatialenable.com";
const REPO = "https://github.com/sgavathe/aws-docker-tf-actions-sample";
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const OUT = resolve(dirname(fileURLToPath(import.meta.url)), "../public");

const PAGES = [
  { src: "README.md", out: "docs/index.html", path: "/docs/index.html",
    title: "Grid Cascade and Harbor Watch: project overview",
    description: "Open-source map app that shows how failures cascade across power, water and communications infrastructure, built with React, ArcGIS, .NET and AWS serverless." },
  { src: "docs/grid-cascade.md", out: "docs/grid-cascade.html", path: "/docs/grid-cascade.html",
    title: "Grid Cascade: how the infrastructure failure model works",
    description: "Draw an area and see which power, water and communications assets fail next. How the cascade algorithm works and how the dependency graph is built from OpenStreetMap." },
  { src: "docs/hosting.md", out: "docs/hosting.html", path: "/docs/hosting.html",
    title: "Hosting a .NET map app on AWS: ECS Fargate vs Lambda serverless",
    description: "Moving from ECS Fargate and an ALB (about $75 a month) to CloudFront, S3 and Lambda (about $3 a month): architecture, cost, wiring without a VPC, and cold starts." },
  { src: "SECURITY.md", out: "docs/security.html", path: "/docs/security.html",
    title: "Security checks for a public Terraform and GitHub Actions repo",
    description: "Six required pull-request checks: gitleaks, sensitive-content scanning, Checkov, OPA policy tests, Terraform plan and unit tests, with accepted risks documented." },
];
const bySrc = new Map(PAGES.map((p) => [p.src, p]));

const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

function rewriteHref(href, fromSrc) {
  if (/^([a-z]+:|#|\/\/)/i.test(href)) return href;               // absolute URL or in-page anchor
  const [file, anchor = ""] = href.split("#");
  const target = posix.normalize(posix.join(posix.dirname(fromSrc), file));
  const hash = anchor ? `#${anchor}` : "";
  const page = bySrc.get(target);
  if (page) return page.path + hash;
  return `${REPO}/blob/main/${target}${hash}`;
}

function render(page) {
  const md = readFileSync(join(ROOT, page.src), "utf8");
  const marked = new Marked({ gfm: true });
  marked.use({
    renderer: {
      code({ text, lang }) {
        if (lang === "mermaid") return `<pre class="mermaid">${esc(text)}</pre>\n`;
        return `<pre><code${lang ? ` class="language-${esc(lang)}"` : ""}>${esc(text)}</code></pre>\n`;
      },
    },
    walkTokens(token) {
      if (token.type === "link") token.href = rewriteHref(token.href, page.src);
    },
  });
  const body = marked.parse(md);
  const nav = PAGES.map((p) => `<a href="${p.path}"${p === page ? ' aria-current="page"' : ""}>${esc(
    { "README.md": "Overview", "docs/grid-cascade.md": "How it works", "docs/hosting.md": "Hosting", "SECURITY.md": "Security" }[p.src],
  )}</a>`).join("");
  const url = SITE + page.path;
  const jsonLd = JSON.stringify({
    "@context": "https://schema.org", "@type": "TechArticle",
    headline: page.title, description: page.description, url,
    author: { "@type": "Person", name: "Shailesh Gavathe" },
    publisher: { "@type": "Organization", name: "SpatialEnable", url: "https://spatialenable.com" },
    dateModified: new Date().toISOString().slice(0, 10),
    isBasedOn: REPO,
  });
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(page.title)}</title>
<meta name="description" content="${esc(page.description)}">
<link rel="canonical" href="${url}">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/favicon.ico" sizes="32x32">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta property="og:type" content="article">
<meta property="og:title" content="${esc(page.title)}">
<meta property="og:description" content="${esc(page.description)}">
<meta property="og:url" content="${url}">
<meta property="og:site_name" content="SpatialEnable">
<meta name="twitter:card" content="summary">
<script type="application/ld+json">${jsonLd}</script>
<style>
  :root { --ink:#1b2430; --quiet:#5b6672; --line:#d9dee4; --bg:#fff; --code:#f4f6f8; --link:#0b5cad; }
  @media (prefers-color-scheme: dark) { :root { --ink:#e6e9ed; --quiet:#9aa5b1; --line:#2c343d; --bg:#11161c; --code:#1a2129; --link:#6cb2ff; } }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif; }
  header { border-bottom:1px solid var(--line); padding:12px 16px; display:flex; flex-wrap:wrap; gap:8px 20px; align-items:center; }
  header .brand { font-weight:700; color:var(--ink); text-decoration:none; }
  header nav { display:flex; flex-wrap:wrap; gap:4px 16px; }
  header nav a { color:var(--quiet); text-decoration:none; }
  header nav a[aria-current="page"] { color:var(--ink); font-weight:600; }
  header .cta { margin-left:auto; background:var(--link); color:#fff; padding:6px 12px; border-radius:6px; text-decoration:none; font-weight:600; }
  main { max-width:880px; margin:0 auto; padding:24px 16px 64px; }
  a { color:var(--link); }
  h1 { font-size:2rem; line-height:1.2; } h2 { margin-top:2.2em; border-bottom:1px solid var(--line); padding-bottom:4px; }
  code { background:var(--code); padding:1px 5px; border-radius:4px; font-size:.9em; }
  pre { background:var(--code); padding:12px 14px; border-radius:6px; overflow-x:auto; }
  pre code { background:none; padding:0; }
  pre.mermaid { background:none; text-align:center; }
  table { border-collapse:collapse; width:100%; display:block; overflow-x:auto; font-size:.95em; }
  th, td { border:1px solid var(--line); padding:6px 10px; text-align:left; vertical-align:top; }
  th { background:var(--code); }
  footer { max-width:880px; margin:0 auto; padding:0 16px 32px; color:var(--quiet); font-size:.9em; }
</style>
</head>
<body>
<header>
  <a class="brand" href="/docs/index.html">Grid Cascade</a>
  <nav aria-label="Documentation">${nav}</nav>
  <a class="cta" href="/">Open the map</a>
</header>
<main>
${body}
</main>
<footer>Source on <a href="${REPO}">GitHub</a>. Map data © OpenStreetMap contributors (ODbL). Dependencies are inferred, not utility records.</footer>
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
  mermaid.initialize({ startOnLoad: true, theme: matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "default" });
</script>
</body>
</html>
`;
}

// Docker builds of frontend/ alone (the ECS image) don't have the repo-root docs: skip quietly.
if (!PAGES.every((p) => existsSync(join(ROOT, p.src)))) {
  console.log("build-docs: repo docs not found (frontend-only build context), skipping");
  process.exit(0);
}

for (const page of PAGES) {
  const file = join(OUT, page.out);
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, render(page));
}

const today = new Date().toISOString().slice(0, 10);
const urls = [{ path: "/", priority: "1.0" }, ...PAGES.map((p) => ({ path: p.path, priority: "0.8" }))];
writeFileSync(join(OUT, "sitemap.xml"), `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls.map((u) => `  <url><loc>${SITE}${u.path}</loc><lastmod>${today}</lastmod><priority>${u.priority}</priority></url>`).join("\n")}
</urlset>
`);

console.log(`build-docs: ${PAGES.length} pages + sitemap.xml -> ${normalize(OUT)}`);
