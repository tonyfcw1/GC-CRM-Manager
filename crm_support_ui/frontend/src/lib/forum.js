export const FORUM_TITLE_MAX_LENGTH = 80;
export const FORUM_COOKIE_STORAGE_KEY = "crm-support-ui.forum-cookie";
export const MAX_FORUM_ATTACHMENTS = 10;
export const MAX_FORUM_ATTACHMENT_SIZE = 10 * 1024 * 1024;
export const MAX_FORUM_TOTAL_ATTACHMENT_SIZE = 50 * 1024 * 1024;

const FORUM_IMAGE_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "image/bmp",
]);
const FORUM_IMAGE_EXTENSIONS = /\.(?:png|jpe?g|gif|webp|bmp)$/i;

function getBrowserStorage() {
  try {
    return globalThis.localStorage;
  } catch {
    return null;
  }
}

export function loadStoredForumCookie(storage = getBrowserStorage()) {
  try {
    return String(storage?.getItem(FORUM_COOKIE_STORAGE_KEY) || "").trim();
  } catch {
    return "";
  }
}

export function saveStoredForumCookie(cookie, storage = getBrowserStorage()) {
  const value = String(cookie || "").trim();
  try {
    if (!storage) return false;
    if (value) {
      storage.setItem(FORUM_COOKIE_STORAGE_KEY, value);
    } else {
      storage.removeItem(FORUM_COOKIE_STORAGE_KEY);
    }
    return true;
  } catch {
    return false;
  }
}

export function clearStoredForumCookie(storage = getBrowserStorage()) {
  try {
    if (!storage) return false;
    storage.removeItem(FORUM_COOKIE_STORAGE_KEY);
    return true;
  } catch {
    return false;
  }
}

export function isForumImageFile(file) {
  const type = String(file?.type || "").split(";", 1)[0].toLowerCase();
  if (FORUM_IMAGE_TYPES.has(type)) return true;
  return !type && FORUM_IMAGE_EXTENSIONS.test(String(file?.name || ""));
}

export function formatForumFileSize(value) {
  const size = Number(value) || 0;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function normalizedText(value) {
  return String(value || "").replace(/\r\n/g, "\n").trim();
}

export function buildForumContent({ description = "" } = {}) {
  // Forum posts intentionally contain only the text entered for the post.
  // CRM association metadata must never be copied into the public topic.
  return normalizedText(description);
}
