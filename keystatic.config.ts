import { config, fields, collection, singleton } from '@keystatic/core';
import { block } from '@keystatic/core/content-components';
import { KeystaticMediaPreview } from './src/components/KeystaticMediaPreview';

const mediaComponents = {
  media: block({
    label: 'Media (Image / Video / YouTube / Vimeo)',
    schema: {
      type: fields.select({
        label: 'Type',
        options: [
          { label: 'Image (media_local/images)', value: 'image' },
          { label: 'Video (media_local/videos)', value: 'video' },
        ],
        defaultValue: 'image',
      }),
      src: fields.text({
        label: 'File Name, Path or URL',
        description: 'e.g. pic.webp, clip.mp4, full media_local path, YouTube or Vimeo URL',
        validation: { isRequired: true },
      }),
      caption: fields.text({ label: 'Caption / Alt Text (optional)' }),
      width: fields.text({ label: 'Max Width (px)', defaultValue: '1680' }),
    },
    NodeView: KeystaticMediaPreview,
  }),
};

export default config({
  storage: {
    kind: 'local',
  },
  ui: {
    brand: {
      name: 'ArtDanylov CMS',
    },
  },
  collections: {
    works: collection({
      label: 'Portfolio Works',
      slugField: 'title',
      path: 'src/content/works/*',
      previewUrl: '/works/{slug}.html',
      format: { contentField: 'content' },
      schema: {
        publishing: fields.object(
          {
            published: fields.checkbox({
              label: 'Published',
              defaultValue: true,
            }),
            publishToRss: fields.checkbox({
              label: 'Push to RSS',
              defaultValue: false,
            }),
          },
          {
            layout: [3, 9],
          }
        ),
        title: fields.slug({ name: { label: 'Title' } }),
        cardSubtitle: fields.text({
          label: 'Card Subtitle',
          description: 'e.g. Character rigging · Playground Labs',
        }),
        lead: fields.text({
          label: 'Lead Summary',
          multiline: true,
        }),
        thumbnail: fields.text({
          label: 'Thumbnail Image',
          description: 'e.g. images/drone-galaxy.webp or full https://... URL',
        }),
        details: fields.object(
          {
            client: fields.text({ label: 'Client' }),
            role: fields.text({ label: 'Role' }),
            tools: fields.text({ label: 'Tools' }),
          },
          {
            label: 'Project Details',
            layout: [4, 4, 4],
          }
        ),
        content: fields.markdoc({
          label: 'Article Content',
          components: mediaComponents,
        }),
      },
    }),

    articles: collection({
      label: 'Blog Articles',
      slugField: 'title',
      path: 'src/content/articles/*',
      previewUrl: '/articles/{slug}.html',
      format: { contentField: 'content' },
      schema: {
        publishing: fields.object(
          {
            published: fields.checkbox({
              label: 'Published',
              defaultValue: true,
            }),
            publishToRss: fields.checkbox({
              label: 'Push to RSS',
              defaultValue: false,
            }),
          },
          {
            layout: [3, 9],
          }
        ),
        title: fields.slug({ name: { label: 'Title' } }),
        date: fields.date({
          label: 'Publish Date',
          validation: { isRequired: true },
        }),
        coverImage: fields.text({
          label: 'Cover Image',
          description: 'e.g. images/cover.webp or full https://... URL',
        }),
        content: fields.markdoc({
          label: 'Article Content',
          components: mediaComponents,
        }),
      },
    }),
  },
  singletons: {
    homePage: singleton({
      label: 'Home Page',
      path: 'src/content/settings/home',
      previewUrl: '/',
      format: { data: 'json' },
      schema: {
        heroTitle: fields.text({ label: 'Hero Title', defaultValue: 'WELCOME' }),
        heroLeadBold: fields.text({ label: 'Subtitle (Bold part)' }),
        heroLeadRest: fields.text({ label: 'Subtitle (Regular part)' }),
        reelButtonLabel: fields.text({ label: 'Red Button Label', defaultValue: 'Reel View' }),
        reelVideo: fields.text({
          label: 'Background & Reel Video',
          description: 'e.g. videos/portfolio.mp4 or full https://... URL',
        }),
        marqueeItems: fields.array(fields.text({ label: 'Skill / Tag' }), {
          label: 'Marquee Ticker Items',
          itemLabel: (props) => props.value || 'Item',
        }),
      },
    }),
    contactPage: singleton({
      label: 'Contact Page',
      path: 'src/content/settings/contact',
      previewUrl: '/contact.html',
      format: { data: 'json' },
      schema: {
        tag: fields.text({ label: 'Section Tag', defaultValue: 'Contact' }),
        heading: fields.text({ label: 'Heading' }),
        intro: fields.text({ label: 'Intro Text', multiline: true }),
        linkedin: fields.object(
          {
            label: fields.text({ label: 'Label' }),
            url: fields.text({ label: 'URL' }),
          },
          { label: 'LinkedIn', layout: [5, 7] }
        ),
        instagram: fields.object(
          {
            label: fields.text({ label: 'Label' }),
            url: fields.text({ label: 'URL' }),
          },
          { label: 'Instagram', layout: [5, 7] }
        ),
        discord: fields.object(
          {
            label: fields.text({ label: 'Label' }),
            url: fields.text({ label: 'URL' }),
          },
          { label: 'Discord', layout: [5, 7] }
        ),
        artstation: fields.object(
          {
            label: fields.text({ label: 'Label' }),
            url: fields.text({ label: 'URL' }),
          },
          { label: 'ArtStation', layout: [5, 7] }
        ),
      },
    }),
    portfolioOrder: singleton({
      label: 'Portfolio Order',
      path: 'src/content/settings/portfolio-order',
      previewUrl: '/portfolio.html',
      format: { data: 'json' },
      schema: {
        items: fields.array(
          fields.relationship({
            label: 'Work',
            collection: 'works',
            validation: { isRequired: true },
          }),
          {
            label: 'Works Order (drag rows to reorder)',
            itemLabel: (props) => props.value || 'Select work',
          }
        ),
      },
    }),
  },
});
