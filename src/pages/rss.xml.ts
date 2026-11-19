import type { APIRoute } from 'astro';
import { getCollection } from 'astro:content';
import { resolveThumbnailUrls } from '../utils/media';
import { getOrderedWorks } from '../utils/works';

const SITE = 'https://artdanylov.com';

function escapeXml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;');
}

function toAbsoluteUrl(url: string): string {
  if (!url) return '';
  if (/^https?:\/\//i.test(url)) return url;
  return `${SITE}${url.startsWith('/') ? '' : '/'}${url}`;
}

function guessImageMime(url: string): string {
  const clean = url.split('?')[0].toLowerCase();
  if (clean.endsWith('.png')) return 'image/png';
  if (clean.endsWith('.jpg') || clean.endsWith('.jpeg')) return 'image/jpeg';
  if (clean.endsWith('.gif')) return 'image/gif';
  return 'image/webp';
}

interface RssItem {
  title: string;
  link: string;
  description: string;
  image?: string;
  pubDate?: Date;
  category: string;
}

export const GET: APIRoute = async () => {
  const [works, articles] = await Promise.all([
    getOrderedWorks(false),
    getCollection('articles'),
  ]);

  const workItems: RssItem[] = works
    .filter((w) => w.data.publishing.published && w.data.publishing.publishToRss)
    .map((w) => ({
      title: w.data.title,
      link: `${SITE}/works/${w.id}.html`,
      description: w.data.lead || w.data.cardSubtitle || `${w.data.title} — 3D portfolio work by Artem Danylov.`,
      image: w.data.thumbnail ? toAbsoluteUrl(resolveThumbnailUrls(w.data.thumbnail).staticUrl) : undefined,
      category: 'Portfolio',
    }));

  const articleItems: RssItem[] = articles
    .filter((a) => a.data.publishing.published && a.data.publishing.publishToRss)
    .sort((a, b) => b.data.date.getTime() - a.data.date.getTime())
    .map((a) => ({
      title: a.data.title,
      link: `${SITE}/articles/${a.id}.html`,
      description: `${a.data.title} — notes on 3D animation, rigging and pipeline by Artem Danylov.`,
      image: a.data.coverImage ? toAbsoluteUrl(resolveThumbnailUrls(a.data.coverImage).staticUrl) : undefined,
      pubDate: a.data.date,
      category: 'Blog',
    }));

  const items: RssItem[] = [...articleItems, ...workItems];

  const itemsXml = items
    .map((item) => {
      const pubDateTag = item.pubDate ? `\n      <pubDate>${item.pubDate.toUTCString()}</pubDate>` : '';
      const enclosureTag = item.image
        ? `\n      <enclosure url="${escapeXml(item.image)}" length="0" type="${guessImageMime(item.image)}" />`
        : '';
      return `    <item>
      <title>${escapeXml(item.title)}</title>
      <link>${escapeXml(item.link)}</link>
      <guid isPermaLink="true">${escapeXml(item.link)}</guid>
      <category>${escapeXml(item.category)}</category>
      <description>${escapeXml(item.description)}</description>${pubDateTag}${enclosureTag}
    </item>`;
    })
    .join('\n');

  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>ArtDanylov — 3D Animation, Rigging &amp; Pipeline</title>
    <link>${SITE}/</link>
    <description>Portfolio works and blog articles by Artem Danylov.</description>
    <language>en</language>
    <atom:link href="${SITE}/rss.xml" rel="self" type="application/rss+xml" />
${itemsXml}
  </channel>
</rss>
`;

  return new Response(xml, {
    headers: {
      'Content-Type': 'application/rss+xml; charset=utf-8',
    },
  });
};
