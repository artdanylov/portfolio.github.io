import { defineMarkdocConfig, component } from '@astrojs/markdoc/config';

export default defineMarkdocConfig({
  tags: {
    media: {
      render: component('./src/components/MarkdocMedia.astro'),
      attributes: {
        type: { type: String },
        src: { type: String, required: true },
        caption: { type: String },
        width: { type: String },
      },
    },
    mediaImage: {
      render: component('./src/components/MarkdocImage.astro'),
      attributes: {
        src: { type: String, required: true },
        alt: { type: String },
      },
    },
    mediaVideo: {
      render: component('./src/components/MarkdocVideo.astro'),
      attributes: {
        src: { type: String, required: true },
        caption: { type: String },
      },
    },
    youtube: {
      render: component('./src/components/MarkdocYoutube.astro'),
      attributes: {
        src: { type: String, required: true },
        title: { type: String },
      },
    },
    vimeo: {
      render: component('./src/components/MarkdocVimeo.astro'),
      attributes: {
        src: { type: String, required: true },
        title: { type: String },
      },
    },
  },
});
