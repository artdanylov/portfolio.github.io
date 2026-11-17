export const MEDIA_CDN_BASE = 'https://media.artdanylov.com';

/**
 * Normalizes a local Windows/POSIX path (e.g. D:\...\media_local\Converted\portfolio\a.webp)
 * into a relative path ("portfolio/a.webp") when possible.
 */
export function normalizeMediaPath(input: string): string {
  const normalized = input.trim().replace(/\\/g, '/');
  const lower = normalized.toLowerCase();
  for (const marker of ['media_local/converted/', 'media_local/', 'converted/']) {
    const idx = lower.indexOf(marker);
    if (idx !== -1) {
      return normalized.slice(idx + marker.length);
    }
  }
  return normalized;
}

/**
 * Resolves a media path:
 * - Full URLs (http/https) and root-relative paths (/...) are returned as-is.
 * - Relative paths (e.g. "main/portfolio.mp4", "portfolio/drone-galaxy/drone-galaxy.webp")
 *   resolve to "/media_local/..." in dev and to "https://media.artdanylov.com/..." in prod.
 */
export function resolveMediaUrl(path?: string | null, _mediaKind?: 'image' | 'video'): string {
  if (!path) return '';
  let cleanInput = normalizeMediaPath(path);
  if (cleanInput.startsWith('http://') || cleanInput.startsWith('https://') || cleanInput.startsWith('/')) {
    return cleanInput;
  }
  cleanInput = cleanInput.replace(/^\.?\//, '');
  if (import.meta.env.DEV) {
    return `/media_local/${cleanInput}`;
  }
  return `${MEDIA_CDN_BASE}/${cleanInput}`;
}
