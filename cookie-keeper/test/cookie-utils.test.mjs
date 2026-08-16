import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import {
  atomicWriteCookie,
  mergeRotatedCookies,
  parseCookieString,
} from "../cookie-utils.mjs";

test("parseCookieString ignores cookie attributes", () => {
  assert.deepEqual(
    parseCookieString("a=1; Path=/; Secure; __Secure-1PSID=x=y"),
    [{ name: "a", value: "1" }, { name: "__Secure-1PSID", value: "x=y" }],
  );
});

test("mergeRotatedCookies changes only Gemini rotation cookies", () => {
  const merged = mergeRotatedCookies(
    "SAPISID=stable; __Secure-1PSID=old; __Secure-1PSIDTS=old-ts; NID=stable-nid",
    [
      { name: "SAPISID", value: "browser-value" },
      { name: "__Secure-1PSID", value: "new" },
      { name: "__Secure-1PSIDTS", value: "new-ts" },
      { name: "__Secure-1PSIDCC", value: "new-cc" },
    ],
  );
  assert.equal(
    merged,
    "SAPISID=stable; __Secure-1PSID=new; __Secure-1PSIDTS=new-ts; " +
      "NID=stable-nid; __Secure-1PSIDCC=new-cc",
  );
});

test("atomicWriteCookie replaces the file with mode 0600", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "cookie-keeper-"));
  const file = path.join(directory, "cookie.txt");
  try {
    await atomicWriteCookie(file, "a=1");
    assert.equal(await fs.readFile(file, "utf8"), "a=1\n");
    assert.equal((await fs.stat(file)).mode & 0o777, 0o600);
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
});
