import fs from "node:fs/promises";
import { chromium } from "playwright";
import {
  atomicWriteCookie,
  mergeRotatedCookies,
  parseCookieString,
} from "./cookie-utils.mjs";

const cookieFile = process.env.COOKIE_FILE || "/session/cookie.txt";
const intervalMs = Math.max(
  60_000,
  Number.parseInt(process.env.REFRESH_INTERVAL_MS || "300000", 10) || 300_000,
);
const settleMs = Math.max(
  0,
  Number.parseInt(process.env.PAGE_SETTLE_MS || "5000", 10) || 5_000,
);
const targetUrl = "https://gemini.google.com/app";
const userAgent = process.env.BROWSER_USER_AGENT ||
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36";

let browser;
let context;
let page;
let loadedCookie = "";
let stopping = false;

function log(message) {
  process.stdout.write(`[cookie-keeper] ${new Date().toISOString()} ${message}\n`);
}

async function readCookieFile() {
  return (await fs.readFile(cookieFile, "utf8")).trim();
}

async function createContext(cookie) {
  if (context) await context.close().catch(() => {});
  context = await browser.newContext({ userAgent });
  const cookies = parseCookieString(cookie).map(({ name, value }) => ({
    name,
    value,
    domain: ".google.com",
    path: "/",
    secure: true,
  }));
  if (!cookies.some(({ name }) => name === "__Secure-1PSID")) {
    throw new Error("cookie file is missing __Secure-1PSID");
  }
  await context.addCookies(cookies);
  page = await context.newPage();
  loadedCookie = cookie;
}

async function refresh() {
  const diskCookie = await readCookieFile();
  if (!context || diskCookie !== loadedCookie) {
    await createContext(diskCookie);
    log("loaded updated credentials into an isolated browser context");
  }

  await page.goto(targetUrl, {
    waitUntil: "domcontentloaded",
    timeout: 60_000,
  });
  if (settleMs) await page.waitForTimeout(settleMs);

  // Match OmniRoute's account-neutral flow: read the live browser jar after
  // Gemini has loaded and persist only the rotatable auth-cookie family.
  const merged = mergeRotatedCookies(loadedCookie, await context.cookies());
  if (merged !== loadedCookie) {
    await atomicWriteCookie(cookieFile, merged);
    loadedCookie = merged;
    log("persisted rotated Gemini session cookies");
  } else {
    log("browser refresh completed; no cookie rotation observed");
  }
}

async function shutdown(signal) {
  if (stopping) return;
  stopping = true;
  log(`received ${signal}; shutting down`);
  await context?.close().catch(() => {});
  await browser?.close().catch(() => {});
  process.exit(0);
}

process.on("SIGTERM", () => void shutdown("SIGTERM"));
process.on("SIGINT", () => void shutdown("SIGINT"));

browser = await chromium.launch({
  headless: true,
  args: ["--disable-dev-shm-usage"],
});
log(`started; refresh interval is ${Math.round(intervalMs / 1000)}s`);

while (!stopping) {
  try {
    await refresh();
  } catch (error) {
    log(`refresh failed: ${error instanceof Error ? error.message : String(error)}`);
  }
  await new Promise((resolve) => setTimeout(resolve, intervalMs));
}
