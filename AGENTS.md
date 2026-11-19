# ArtDanylov Portfolio & Blog (`artdanylov.com`)

## Обзор проекта
Персональный сайт-портфолио и блог (3D animation, rigging, mocap, pipeline).
- **Хостинг сайта:** GitHub Pages (`artdanylov/portfolio.github.io`), деплой через `.github/workflows/deploy.yml`.
- **Медиа-CDN:** Cloudflare R2 (`https://media.artdanylov.com/`, бакет `artdanylov-media`).
- **Технологический стек:**
  - **Astro 5** (`output: 'static'`, `build.format: 'file'`) — генератор статических сайтов (в продакшн уходит чистый HTML/CSS/JS без серверного рантайма и без тяжёлых JS-фреймворков).
  - **Markdoc** (`@astrojs/markdoc`) — формат контента `.mdoc` для проектов и статей с поддержкой кастомного блока `{% media %}`.
  - **Keystatic + React 19** (`@keystatic/astro`, `@astrojs/react`) — локальная CMS, подключается **только в `dev`** (в продакшн-сборку не попадает).
  - **Собственная дизайн-система (CSS/JS без Bootstrap и сторонних UI-библиотек):** `public/assets/css/base.css`, `public/assets/css/styles.min.css`, `public/assets/js/script.min.js`, шрифты Google Fonts (`Outfit` + `Space Grotesk`).
  - **Web3Forms** — отправка формы обратной связи на `/contact.html` без бэкенда.
  - **Python 3 + FFmpeg + boto3** — локальный медиа-пайплайн (`scripts/media_sync.py` и `scripts/release.py`): конвертация видео/картинок, веб-инструмент **Video & Thumbnail Studio**, загрузка в Cloudflare R2 и релиз.

## Структура
- `src/layouts/BaseLayout.astro` — общий каркас (навигация `.site-nav`, футер `.site-footer`, `base.css`, `styles.min.css`, `script.min.js`), а также автоматическое SEO: `<link rel="canonical">`, Open Graph / Twitter Cards (`og:*`, `twitter:*`), JSON-LD `Person` schema (из `contact.json` и `home.json`) и автообнаружение RSS (`/rss.xml`).
- `src/pages/`:
  - Страницы: `index.astro`, `portfolio.astro`, `blog.astro`, `contact.astro`, `404.astro` (`noindex`), `works/[slug].astro`, `articles/[slug].astro`.
  - Автогенерируемые фиды: `sitemap.xml.ts` (`/sitemap.xml`) и `rss.xml.ts` (`/rss.xml`).
- `public/robots.txt` — правила индексации и ссылка на `https://artdanylov.com/sitemap.xml`.
- `src/content/works/*.mdoc` — проекты портфолио (1 общий шаблон `works/[slug].astro`).
- `src/content/articles/*.mdoc` — статьи блога (1 общий шаблон `articles/[slug].astro`).
- `src/content/settings/`: `home.json`, `contact.json`, `portfolio-order.json` (порядок карточек в портфолио редактируется перетаскиванием в Keystatic → **Portfolio Order**).
- `keystatic.config.ts` и `src/content.config.ts` — схемы коллекций. У проектов и статей вверху формы есть строка `publishing` с чекбоксами **Published** (`true` по умолчанию; черновики видны только в `dev` с бейджем `Draft` и полностью исключаются из `build`, `sitemap.xml` и `rss.xml`) и **Push to RSS** (`false` по умолчанию; включает запись в `/rss.xml`).
- `markdoc.config.mjs`, `src/components/MarkdocMedia.astro`, `src/components/KeystaticMediaPreview.tsx` — универсальный блок `{% media %}` для редактора (сам определяет Image / MP4 Video / YouTube / Vimeo по ссылке или `type`).
- `src/utils/media.ts` — `resolveMediaUrl()` и `resolveThumbnailUrls()`:
  - Внешние ссылки (`http://`, `https://`, `/`) не изменяются.
  - Локальные пути в `dev` резолвятся в `/media_local/...`, а в продакшн-сборке — в `https://media.artdanylov.com/...`.
- `media_local/` (в `.gitignore`):
  - `_toConvert/` — исходники видео и картинок (можно раскладывать по подпапкам, например `blog/...`, `portfolio/...`).
  - `Converted/` — сконвертированные `.mp4` (H.264, `yuv420p`, `faststart`, <=1080p/30fps) и `.webp` (в т.ч. из `.gif`), с сохранением структуры подпапок и без пробелов в именах файлов.
- `scripts/media_sync.py` — конвертация новых файлов из `_toConvert/` в `Converted/` (авто-санитайзинг имён, пропуск актуальных файлов), локальный веб-интерфейс **Video & Thumbnail Studio** (`--thumbs`, `http://127.0.0.1:4322/`) для видео и картинок в `Converted/` (просмотр, копирование путей для CMS, генерация `_thumb.webp` `640x360 @ 24fps` или 1-кадрового статичного превью с интерактивным **Crop / Pan & Zoom** и режимом `1:1 (640×360)` без даунскейла, сохранение настроек в `Converted/.thumb_studio_state.json`), и загрузка в Cloudflare R2 (`--upload` для новых, `--upload-all` для всех). Требует `ffmpeg`, `boto3` и `.env` (шаблон `.env.example`).

## Команды и заметки по разработке (Windows)
- В PowerShell использовать `npm.cmd` (т.к. `npm.ps1` может быть заблокирован политикой скриптов).
- Запуск сайта + админки Keystatic: `dev.bat` или `npm.cmd run dev` → сайт на `http://127.0.0.1:4321/`, админка на `http://127.0.0.1:4321/keystatic`.
- В `astro.config.mjs` Keystatic подключается только при `astro dev`; при `npm.cmd run build` собирается чистая статика в `dist/` без админки, без черновиков (`Published = false`) и без dev-кнопок `Edit`.
- `serveLocalMedia()` в `astro.config.mjs` отдаёт `media_local/` в `dev` с поддержкой HTTP `Range` (`206 Partial Content`) для корректной работы `<video>`.
- Конвертация новых медиа из `_toConvert/` в `Converted/` и загрузка в R2: `media_upload.bat` или `python scripts\media_sync.py --upload` (для полной пересинхронизации всех файлов: `python scripts\media_sync.py --upload-all`).
- Открытие **Video & Thumbnail Studio** (с авто-выгрузкой новых превью в R2): `media_thumbs.bat` или `python scripts\media_sync.py --thumbs --upload`.
- Полный релиз сайта (медиа в R2 → проверка `npm.cmd run build` → `git commit` + `git push`): `release.bat` или `python scripts\release.py`.
