import type { APIRoute } from 'astro';
import { getCollection } from 'astro:content';
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

export const GET: APIRoute = async () => {
  const [works, allArticles] = await Promise.all([
    getOrderedWorks(false),
    getCollection('articles'),
  ]);
  const articles = allArticles.filter((a) => a.data.publishing.published);

  const urls: Array<{ loc: string; lastmod?: string; priority: string }> = [
    { loc: `${SITE}/`, priority: '1.0' },
    { loc: `${SITE}/portfolio.html`, priority: '0.9' },
    { loc: `${SITE}/blog.html`, priority: '0.8' },
    { loc: `${SITE}/contact.html`, priority: '0.7' },
    ...works.map((work) => ({
      loc: `${SITE}/works/${work.id}.html`,
      priority: '0.8',
    })),
    ...articles
      .sort((a, b) => b.data.date.getTime() - a.data.date.getTime())
      .map((article) => ({
        loc: `${SITE}/articles/${article.id}.html`,
        lastmod: article.data.date.toISOString().split('T')[0],
        priority: '0.7',
      })),
  ];

  const body = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls
  .map(
    (entry) => `  <url>
    <loc>${escapeXml(entry.loc)}</loc>${entry.lastmod ? `\n    <lastmod>${entry.lastmod}</lastmod>` : ''}
    <priority>${entry.priority}</priority>
  </url>`
  )
  .join('\n')}
</urlset>
`;

  return new Response(body, {
    headers: {
      'Content-Type': 'application/xml; charset=utf-8',
    },
  });
};
