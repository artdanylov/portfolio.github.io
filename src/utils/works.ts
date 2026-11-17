import { getCollection } from 'astro:content';
import fs from 'node:fs';
import path from 'node:path';

export async function getOrderedWorks() {
  const works = await getCollection('works');
  let orderList: string[] = [];

  try {
    const orderFile = path.resolve('src/content/settings/portfolio-order.json');
    if (fs.existsSync(orderFile)) {
      const parsed = JSON.parse(fs.readFileSync(orderFile, 'utf8'));
      if (Array.isArray(parsed.items)) {
        orderList = parsed.items;
      }
    }
  } catch {
    // Fallback to `order` field if JSON cannot be read
  }

  const orderMap = new Map(orderList.map((slug, index) => [slug, index]));

  return works.sort((a, b) => {
    const idxA = orderMap.has(a.id) ? orderMap.get(a.id)! : Number.MAX_SAFE_INTEGER;
    const idxB = orderMap.has(b.id) ? orderMap.get(b.id)! : Number.MAX_SAFE_INTEGER;
    if (idxA !== idxB) return idxA - idxB;
    return a.id.localeCompare(b.id);
  });
}
