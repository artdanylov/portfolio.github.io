import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';

const works = defineCollection({
  loader: glob({ pattern: '**/*.mdoc', base: './src/content/works' }),
  schema: z.object({
    title: z.string(),
    cardSubtitle: z.string().optional().default(''),
    lead: z.string().optional().default(''),
    description: z.string().optional().default(''),
    thumbnail: z.string().optional().default(''),
    details: z
      .object({
        client: z.string().optional().nullable(),
        role: z.string().optional().nullable(),
        tools: z.string().optional().nullable(),
      })
      .optional()
      .default({}),
  }),
});

const articles = defineCollection({
  loader: glob({ pattern: '**/*.mdoc', base: './src/content/articles' }),
  schema: z.object({
    title: z.string(),
    date: z.coerce.date(),
    coverImage: z.string().optional().default(''),
  }),
});

export const collections = { works, articles };
