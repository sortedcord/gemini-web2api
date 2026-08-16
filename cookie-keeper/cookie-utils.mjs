import fs from "node:fs/promises";
import path from "node:path";

export const ROTATABLE_COOKIE_NAMES = new Set([
  "__Secure-1PSID",
  "__Secure-1PSIDTS",
  "__Secure-1PSIDCC",
]);

const COOKIE_ATTRIBUTES = new Set([
  "domain",
  "expires",
  "httponly",
  "max-age",
  "path",
  "samesite",
  "secure",
]);

export function parseCookieString(raw) {
  if (typeof raw !== "string") return [];
  return raw
    .split(";")
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      const separator = part.indexOf("=");
      if (separator <= 0) return null;
      const name = part.slice(0, separator).trim();
      const value = part.slice(separator + 1).trim();
      if (!name || !value || COOKIE_ATTRIBUTES.has(name.toLowerCase())) return null;
      return { name, value };
    })
    .filter(Boolean);
}

export function serializeCookies(cookies) {
  return cookies.map(({ name, value }) => `${name}=${value}`).join("; ");
}

export function mergeRotatedCookies(originalCookie, browserCookies) {
  const original = parseCookieString(originalCookie);
  const rotated = new Map(
    browserCookies
      .filter(({ name, value }) => ROTATABLE_COOKIE_NAMES.has(name) && value)
      .map(({ name, value }) => [name, value]),
  );

  const seen = new Set();
  const merged = original.map(({ name, value }) => {
    seen.add(name);
    return { name, value: rotated.get(name) ?? value };
  });
  for (const name of ROTATABLE_COOKIE_NAMES) {
    if (!seen.has(name) && rotated.has(name)) {
      merged.push({ name, value: rotated.get(name) });
    }
  }
  return serializeCookies(merged);
}

export async function atomicWriteCookie(filePath, value) {
  const directory = path.dirname(filePath);
  const temporary = path.join(
    directory,
    `.cookie.txt.${process.pid}.${Date.now()}.tmp`,
  );
  await fs.writeFile(temporary, `${value.trim()}\n`, { mode: 0o600 });
  await fs.chmod(temporary, 0o600);
  await fs.rename(temporary, filePath);
}
