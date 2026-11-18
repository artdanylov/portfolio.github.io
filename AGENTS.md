# ArtDanylov Portfolio & Blog (`artdanylov.com`)

## Обзор проекта
Персональный сайт-портфолио и блог (3D animation, rigging, mocap, pipeline).
- **Хостинг сайта:** GitHub Pages (`artdanylov/portfolio.github.io`), деплой через `.github/workflows/deploy.yml`.
- **Медиа-CDN:** Cloudflare R2 (`https://media.artdanylov.com/`, бакет `artdanylov-media`).
- **Стек:** Astro 5 (`output: 'static'`, `build.format: 'file'`), Keystatic (локальная CMS только для `dev`), Bootstrap 5 + стили в `public/assets/`.

## Структура
- `src/layouts/BaseLayout.astro` — общий каркас (навигация, футер, Bootstrap, `styles.min.css`, `script.min.js`, AOS).
- `src/pages/` — страницы `index.astro`, `portfolio.astro`, `blog.astro`, `contact.astro`, `404.astro`, `works/[slug].astro`, `articles/[slug].astro`.
- `src/content/works/*.mdoc` — проекты портфолио (1 общий шаблон `works/[slug].astro`).
- `src/content/articles/*.mdoc` — статьи блога (1 общий шаблон `articles/[slug].astro`).
- `src/content/settings/portfolio-order.json` — порядок карточек в портфолио (редактируется перетаскиванием в Keystatic → **Portfolio Order**).
- `keystatic.config.ts` и `src/content.config.ts` — схемы коллекций.
- `markdoc.config.mjs`, `src/components/MarkdocMedia.astro`, `src/components/KeystaticMediaPreview.tsx` — универсальный блок `{% media %}` для редактора (сам определяет Image / MP4 Video / YouTube / Vimeo по ссылке или `type`).
- `src/utils/media.ts` — `resolveMediaUrl()`:
  - Внешние ссылки (`http://`, `https://`, `/`) не изменяются.
  - Локальные имена файлов / пути (`pic.webp`, `images/pic.webp`, `D:\...\media_local\...`) в `dev` резолвятся в `/media_local/...`, а в продакшн-сборке — в `https://media.artdanylov.com/...`.
- `media_local/` (в `.gitignore`):
  - `_toConvert/` — исходники видео и картинок (можно раскладывать по подпапкам, например `blog/...`, `portfolio/...`).
  - `Converted/` — сконвертированные `.mp4` (H.264, `yuv420p`, `faststart`, <=1080p/30fps) и `.webp` (в т.ч. из `.gif`), с сохранением структуры подпапок.
- `scripts/media_sync.py` — конвертация новых файлов из `_toConvert/` в `Converted/` (пропускает файлы, если выходной файл не старее исходника), английский веб-интерфейс **Thumbnail Studio** (`--thumbs`) для уже готовых видео в `Converted/` (просмотр, копирование ссылок на `.mp4`, `_thumb.webp`, `_animthumb.webp` для CMS, генерация `_thumb.webp` `960x540` и `_animthumb.webp` `640x360 @ 24fps` с сохранением настроек в `.thumb_studio_state.json`), и загрузка в Cloudflare R2 (`--upload` для новых, `--upload-all` для всех). Требует `ffmpeg`, `boto3` и `.env` (шаблон `.env.example`).

## Команды и заметки по разработке (Windows)
- В PowerShell использовать `npm.cmd` (т.к. `npm.ps1` может быть заблокирован политикой скриптов).
- Запуск сайта + админки Keystatic: `dev.bat` или `npm.cmd run dev` → сайт на `http://127.0.0.1:4321/`, админка на `http://127.0.0.1:4321/keystatic`.
- В `astro.config.mjs` Keystatic подключается только при `astro dev`; при `npm.cmd run build` собирается чистая статика в `dist/` без админки и без dev-кнопок `Edit`.
- `serveLocalMedia()` в `astro.config.mjs` отдаёт `media_local/` в `dev` с поддержкой HTTP `Range` (`206 Partial Content`) для корректной работы `<video>`.
- Конвертация новых медиа из `_toConvert/` в `Converted/` и загрузка в R2: `media_upload.bat` или `python scripts\media_sync.py --upload`.
- Открытие **Thumbnail Studio** для готовых видео в `Converted/` (с авто-выгрузкой новых превью в R2): `media_thumbs.bat` или `python scripts\media_sync.py --thumbs --upload`.
- Полный релиз сайта (медиа в R2 → проверка `npm.cmd run build` → `git commit` + `git push`): `release.bat` или `python scripts\release.py`.
