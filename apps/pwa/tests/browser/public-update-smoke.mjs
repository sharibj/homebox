import fs from "node:fs";
import https from "node:https";
import tls from "node:tls";
import crypto from "node:crypto";
import { chromium } from "@playwright/test";

const origin = "https://192.168.178.138";
const phase = process.env.PUBLIC_UPDATE_PHASE || "pre";
if (!["pre", "post"].includes(phase)) throw new Error("Unknown public update phase.");
process.umask(0o077);
const root = "/private-profile", profile = `${root}/profile`, home = `${root}/home`;
const ca = fs.readFileSync("/public-ca.crt");
const hash = bytes => crypto.createHash("sha256").update(bytes).digest("hex");
const expected = {
  "/": "d20f83c6b6211ec5d95afd62c35bbac4777b43c2e859527222024b7a8417dda4",
  "/assets/index-DFDIgzJA.js": "df82b8ec0cbc8a8e43ceb5997b99c68afc76239d7be6b5e8513a7d9e84897dc9",
  "/assets/index-D-_kYhpF.css": "3677cd6785bbdf57e4590d1da108e0fcf0fc3542d0b2d11bf28ac14d3500c6cf",
  "/sw.js": "2811e86ffecd20ce0c597148b217151abcb1915810311c0b9822062e938d3a50",
};
function publicGet(path) {
  const url = new URL(path, origin);
  if (url.origin !== origin) throw new Error("Only the approved public origin is allowed.");
  return new Promise((resolve, reject) => {
    const request = https.get(url, {
      ca, rejectUnauthorized: true, agent: false, signal: AbortSignal.timeout(15000),
      checkServerIdentity: (_host, cert) => tls.checkServerIdentity(new URL(origin).hostname, cert),
    }, response => {
      const chunks = [];
      const certificate = new crypto.X509Certificate(response.socket.getPeerCertificate().raw);
      const leafSpki = crypto.createHash("sha256").update(certificate.publicKey.export({ type: "spki", format: "der" })).digest("base64");
      const authorized = response.socket.authorized;
      response.on("data", chunk => chunks.push(chunk));
      response.on("end", () => resolve({ path: url.pathname, status: response.statusCode, body: Buffer.concat(chunks), authorized, leafSpki }));
      response.on("error", reject);
    });
    request.setTimeout(15000, () => request.destroy(new Error("Public TLS request timed out.")));
    request.on("error", reject);
  });
}
let context;
const observation = { phase, origin, profileOutsideRepository: true, hostTrustChanged: false, broadCertificateBypass: false, qaAuthReused: false };
try {
  if ((fs.statSync(root).mode & 0o777) !== 0o700) throw new Error("Anonymous root must be 0700.");
  if (phase === "pre" && fs.existsSync(profile) && fs.readdirSync(profile).length) throw new Error("Pre-deployment needs the one new empty anonymous profile.");
  if (phase === "post" && !fs.existsSync(`${root}/pre-ready.json`)) throw new Error("The original pre-deployment profile is required.");
  fs.mkdirSync(home, { recursive: true, mode: 0o700 });
  fs.mkdirSync(profile, { recursive: true, mode: 0o700 });
  observation.stage = "public-tls-landing";
  const html = await publicGet("/");
  if (html.status !== 200 || !html.authorized) throw new Error("Public landing TLS or HTTP failed.");
  const script = html.body.toString().match(/src=["'](\/assets\/[^"']+\.js)["']/)?.[1];
  const css = html.body.toString().match(/href=["'](\/assets\/[^"']+\.css)["']/)?.[1];
  if (!script || !css) throw new Error("Public landing has no expected static entry.");
  observation.stage = "public-static-files";
  const files = [html];
  for (const path of [script, css, "/sw.js"]) files.push(await publicGet(path));
  if (files.some(file => file.status !== 200 || !file.authorized || file.leafSpki !== html.leafSpki)) throw new Error("Public static TLS/HTTP check failed.");
  const session = await publicGet("/api/assistant/v1/session");
  if (session.status !== 401) throw new Error("Anonymous public bootstrap must return 401.");
  observation.assets = files.map(file => ({ path: file.path, status: file.status, bytes: file.body.length, sha256: hash(file.body) }));
  observation.trust = { suppliedPublicCaVerified: true, hostnameVerified: true, chromiumTrust: "Exact verified leaf-SPKI pin only", leafSpkiSha256: html.leafSpki };
  observation.anonymousBootstrapStatus = session.status;
  observation.stage = "anonymous-browser";
  context = await chromium.launchPersistentContext(profile, {
    headless: true, viewport: { width: 390, height: 844 }, ignoreHTTPSErrors: false,
    args: [`--ignore-certificate-errors-spki-list=${html.leafSpki}`], serviceWorkers: "allow",
  });
  if ((await context.cookies()).length !== 0) throw new Error("The persistent profile must remain anonymous.");
  const blocked = [], browserBootstrap = [];
  await context.route("**/*", route => {
    const request = route.request(), url = new URL(request.url());
    const allowedPath = url.pathname === "/" || url.pathname === "/sw.js" || url.pathname === "/manifest.webmanifest"
      || url.pathname.startsWith("/assets/") || /\.(png|svg|ico)$/.test(url.pathname)
      || url.pathname === "/api/assistant/v1/session";
    if (request.method() !== "GET" || url.origin !== origin || !allowedPath) {
      blocked.push({ method: request.method(), path: url.pathname }); return route.abort();
    }
    return route.continue();
  });
  const page = context.pages()[0] || await context.newPage();
  page.on("response", response => { if (new URL(response.url()).pathname === "/api/assistant/v1/session") browserBootstrap.push(response.status()); });
  await page.goto(origin, { waitUntil: "domcontentloaded", timeout: 20000 });
  if (phase === "post") await page.reload({ waitUntil: "domcontentloaded", timeout: 20000 });
  const username = page.locator('input[autocomplete="username"]');
  const password = page.locator('input[autocomplete="current-password"]');
  await username.waitFor({ state: "visible", timeout: 15000 });
  if (await username.inputValue() || await password.inputValue()) throw new Error("Anonymous login fields must stay empty.");
  observation.browserBundle = await page.locator('script[type="module"][src]').getAttribute("src");
  observation.loginVisible = true;
  observation.browserBootstrapStatuses = browserBootstrap;
  observation.serviceWorker = await page.evaluate(async () => {
    const result = { secureContext: isSecureContext, supported: "serviceWorker" in navigator };
    if (!result.supported) return result;
    let timer;
    try { await Promise.race([navigator.serviceWorker.ready, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("No active worker yet")), 5000); })]); }
    catch { result.readinessLimit = "No active worker observed within the bounded visit."; }
    finally { clearTimeout(timer); }
    result.registrations = (await navigator.serviceWorker.getRegistrations()).map(registration => ({
      scope: new URL(registration.scope).pathname, active: registration.active?.scriptURL ? new URL(registration.active.scriptURL).pathname : null,
      activeState: registration.active?.state || null, waiting: Boolean(registration.waiting), installing: Boolean(registration.installing),
    }));
    result.controller = navigator.serviceWorker.controller ? new URL(navigator.serviceWorker.controller.scriptURL).pathname : null;
    result.caches = [];
    for (const name of await caches.keys()) {
      const urls = (await (await caches.open(name)).keys()).map(request => new URL(request.url()).pathname);
      result.caches.push({ name, paths: urls });
    }
    return result;
  });
  observation.blockedRequests = blocked;
  observation.cookiesPresent = (await context.cookies()).length !== 0;
  if (!browserBootstrap.includes(401) || blocked.length || observation.cookiesPresent) throw new Error("The visit did not remain a clean public-only anonymous bootstrap.");
  if (phase === "post" && (observation.browserBundle !== "/assets/index-DFDIgzJA.js"
    || observation.assets.some(file => expected[file.path] !== file.sha256))) throw new Error("Normal reload did not show the final frozen frontend.");
  observation.profileMode = ((fs.statSync(profile).mode) & 0o777).toString(8);
  await page.screenshot({ path: `/results/public-${phase}-anonymous-login.png`, fullPage: true });
  await context.close(); context = null;
  observation.browserClosed = true;
  observation.status = phase === "pre" ? "PUBLIC_PRE_PROFILE_READY" : "PUBLIC_POST_NORMAL_RELOAD_PASS";
  fs.writeFileSync(`${root}/${phase}-ready.json`, JSON.stringify(observation, null, 2), { mode: 0o600 });
  fs.writeFileSync(`/results/public-${phase}-proof.json`, JSON.stringify(observation, null, 2));
  console.log(JSON.stringify(observation));
} catch (cause) {
  observation.status = "BOUNDED_PUBLIC_CHECK_STOP";
  observation.errorType = cause.name;
  observation.errorCode = cause.code || null;
  observation.detail = String(cause.message).split("\n")[0].slice(0, 300);
  observation.limitation = "The public-only browser or trusted TLS check did not complete. No private inventory/login action was attempted.";
  fs.writeFileSync(`/results/public-${phase}-stop.json`, JSON.stringify(observation, null, 2));
  console.log(JSON.stringify(observation));
  process.exitCode = 1;
} finally { await context?.close(); }
