import { defineConfig } from 'astro/config';
import react from '@astrojs/react';
import markdoc from '@astrojs/markdoc';
import keystatic from '@keystatic/astro';
import fs from 'node:fs';
import path from 'node:path';

const isDev = process.argv.includes('dev');

/** Recursively finds a file by name (case-insensitive) inside rootDir */
function findFileRecursive(rootDir, targetName) {
  if (!fs.existsSync(rootDir)) return null;
  const lowerTarget = targetName.toLowerCase();
  const stack = [rootDir];
  while (stack.length > 0) {
    const dir = stack.pop();
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        stack.push(full);
      } else if (entry.name.toLowerCase() === lowerTarget) {
        return full;
      }
    }
  }
  return null;
}

/** Serves ./media_local/Converted/* at /media_local/* during `astro dev` */
function serveLocalMedia() {
  return {
    name: 'serve-local-media',
    configureServer(server) {
      const convertedRoot = path.resolve('media_local', 'Converted');
      const mediaRoot = path.resolve('media_local');
      server.middlewares.use('/media_local', (req, res, next) => {
        const [rawPath, rawQuery = ''] = (req.url || '/').split('?');
        if (rawPath === '/_resolve') {
          const params = new URLSearchParams(rawQuery);
          const name = (params.get('name') || '').trim();
          const baseName = path.basename(name.replace(/\\/g, '/'));
          const found = baseName ? findFileRecursive(convertedRoot, baseName) : null;
          const rel = found ? path.relative(convertedRoot, found).replace(/\\/g, '/') : null;
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ path: rel }));
          return;
        }

        const decoded = decodeURIComponent(rawPath).replace(/^\/+/, '');
        let filePath = path.join(convertedRoot, decoded);
        if (!filePath.startsWith(convertedRoot) || !fs.existsSync(filePath) || fs.statSync(filePath).isDirectory()) {
          filePath = path.join(mediaRoot, decoded);
          if (!filePath.startsWith(mediaRoot) || !fs.existsSync(filePath) || fs.statSync(filePath).isDirectory()) {
            const byName = findFileRecursive(convertedRoot, path.basename(decoded));
            if (!byName) return next();
            filePath = byName;
          }
        }
        const stat = fs.statSync(filePath);
        const ext = path.extname(filePath).toLowerCase();
        const mimeTypes = {
          '.mp4': 'video/mp4',
          '.webm': 'video/webm',
          '.webp': 'image/webp',
          '.jpg': 'image/jpeg',
          '.jpeg': 'image/jpeg',
          '.png': 'image/png',
        };
        const contentType = mimeTypes[ext] || 'application/octet-stream';
        const range = req.headers.range;

        if (range) {
          const parts = range.replace(/bytes=/, '').split('-');
          const start = parseInt(parts[0], 10);
          const end = parts[1] ? parseInt(parts[1], 10) : stat.size - 1;
          const chunkSize = end - start + 1;
          res.writeHead(206, {
            'Content-Range': `bytes ${start}-${end}/${stat.size}`,
            'Accept-Ranges': 'bytes',
            'Content-Length': chunkSize,
            'Content-Type': contentType,
          });
          fs.createReadStream(filePath, { start, end }).pipe(res);
        } else {
          res.writeHead(200, {
            'Accept-Ranges': 'bytes',
            'Content-Length': stat.size,
            'Content-Type': contentType,
          });
          fs.createReadStream(filePath).pipe(res);
        }
      });
    },
  };
}

export default defineConfig({
  site: 'https://artdanylov.com',
  output: 'static',
  build: {
    format: 'file',
  },
  integrations: isDev ? [react(), markdoc(), keystatic()] : [markdoc()],
  vite: {
    plugins: isDev ? [serveLocalMedia()] : [],
  },
});
