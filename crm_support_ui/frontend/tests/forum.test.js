import test from "node:test";
import assert from "node:assert/strict";

import {
  buildForumContent,
  clearStoredForumCookie,
  formatForumFileSize,
  loadStoredForumCookie,
  isForumImageFile,
  MAX_FORUM_ATTACHMENTS,
  MAX_FORUM_ATTACHMENT_SIZE,
  MAX_FORUM_TOTAL_ATTACHMENT_SIZE,
  saveStoredForumCookie,
} from "../src/lib/forum.js";
import { crmApi } from "../src/lib/crm.js";


test("keeps forum body line breaks without adding CRM association metadata", () => {
  assert.equal(
    buildForumContent({
      description: "第一行\r\n第二行",
      sourceName: "Acme",
      actualEnd: "2026-09-04",
      crmUrl: "https://crm.example/case/1",
    }),
    "第一行\n第二行",
  );
});

test("stores and clears a forum cookie through the browser storage helpers", () => {
  const values = new Map();
  const storage = {
    getItem(key) {
      return values.get(key) ?? null;
    },
    setItem(key, value) {
      values.set(key, String(value));
    },
    removeItem(key) {
      values.delete(key);
    },
  };

  assert.equal(loadStoredForumCookie(storage), "");
  assert.equal(saveStoredForumCookie("  sid=abc; token=xyz  ", storage), true);
  assert.equal(loadStoredForumCookie(storage), "sid=abc; token=xyz");
  assert.equal(clearStoredForumCookie(storage), true);
  assert.equal(loadStoredForumCookie(storage), "");
});

test("recognizes supported clipboard image files and formats their sizes", () => {
  const file = new File([new Uint8Array([1, 2, 3])], "shot.png", { type: "image/png" });
  assert.equal(isForumImageFile(file), true);
  assert.equal(isForumImageFile(new File(["x"], "notes.txt", { type: "text/plain" })), false);
  assert.equal(formatForumFileSize(1024 * 1024), "1.0 MB");
  assert.equal(MAX_FORUM_ATTACHMENTS, 10);
  assert.equal(MAX_FORUM_ATTACHMENT_SIZE, 10 * 1024 * 1024);
  assert.equal(MAX_FORUM_TOTAL_ATTACHMENT_SIZE, 50 * 1024 * 1024);
});

test("uses multipart only when forum images are present", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (path, options) => {
    requests.push({ path, options });
    return { ok: true, async json() { return { url: "https://gcdn.example/topic" }; } };
  };
  try {
    await crmApi.createForumPost({ cookie: "sid=abc", title: "主题", content: "正文" });
    const image = new File([new Uint8Array([1, 2, 3])], "shot.png", { type: "image/png" });
    await crmApi.createForumPost({
      cookie: "sid=abc",
      title: "主题",
      content: "正文",
      attachments: [{ file: image }],
    });
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(requests[0].path, "/api/forum-post");
  assert.equal(requests[0].options.headers["Content-Type"], "application/json");
  assert.equal(requests[1].path, "/api/forum-post-with-images");
  assert.equal(requests[1].options.headers["Content-Type"], undefined);
  assert.equal(requests[1].options.body.get("cookie"), "sid=abc");
  assert.equal(requests[1].options.body.get("attachments").name, "shot.png");
});
