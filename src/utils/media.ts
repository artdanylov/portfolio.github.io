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

/**
 * Resolves both static (_thumb.webp) and animated (_animthumb.webp) thumbnail URLs
 * for portfolio and blog cards.
 * Accepts:
 * - "folder/clip_thumb.webp" -> static: "folder/clip_thumb.webp", anim: "folder/clip_animthumb.webp"
 * - "folder/clip.mp4"        -> static: "folder/clip_thumb.webp", anim: "folder/clip_animthumb.webp"
 * - "folder/pic.webp"        -> static: "folder/pic.webp",        anim: "folder/pic_animthumb.webp"
 */
export function resolveThumbnailUrls(path?: string | null): { staticUrl: string; animUrl: string } {
  if (!path) return { staticUrl: '', animUrl: '' };
  const clean = normalizeMediaPath(path);
  if (!clean) return { staticUrl: '', animUrl: '' };

  if (/\.(mp4|mov|webm|m4v|avi|mkv)$/i.test(clean)) {
    const base = clean.replace(/\.(mp4|mov|webm|m4v|avi|mkv)$/i, '');
    return {
      staticUrl: resolveMediaUrl(`${base}_thumb.webp`, 'image'),
      animUrl: resolveMediaUrl(`${base}_animthumb.webp`, 'image'),
    };
  }

  if (/_thumb\.webp$/i.test(clean)) {
    const animPath = clean.replace(/_thumb\.webp$/i, '_animthumb.webp');
    return {
      staticUrl: resolveMediaUrl(clean, 'image'),
      animUrl: resolveMediaUrl(animPath, 'image'),
    };
  }

  if (/_animthumb\.webp$/i.test(clean)) {
    const staticPath = clean.replace(/_animthumb\.webp$/i, '_thumb.webp');
    return {
      staticUrl: resolveMediaUrl(staticPath, 'image'),
      animUrl: resolveMediaUrl(clean, 'image'),
    };
  }

  if (/\.webp$/i.test(clean)) {
    const animPath = clean.replace(/\.webp$/i, '_animthumb.webp');
    return {
      staticUrl: resolveMediaUrl(clean, 'image'),
      animUrl: resolveMediaUrl(animPath, 'image'),
    };
  }

  return {
    staticUrl: resolveMediaUrl(clean, 'image'),
    animUrl: '',
  };
}
