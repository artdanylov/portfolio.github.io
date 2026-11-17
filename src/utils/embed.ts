export function toYoutubeEmbed(src: string, autoplay = false): string {
  if (src.includes('youtube.com/embed/') || src.includes('youtube-nocookie.com/embed/')) {
    return src;
  }
  const match = src.match(/(?:youtu\.be\/|v=)([A-Za-z0-9_-]{11})/);
  const id = match ? match[1] : src.trim();
  const params = autoplay ? 'rel=0&autoplay=1&mute=1&playsinline=1' : 'rel=0';
  return `https://www.youtube-nocookie.com/embed/${id}?${params}`;
}

export function toVimeoEmbed(src: string): string {
  if (src.includes('player.vimeo.com/video/')) {
    return src;
  }
  const match = src.match(/vimeo\.com\/(?:video\/)?(\d+)/);
  const id = match ? match[1] : src.trim();
  return `https://player.vimeo.com/video/${id}`;
}
