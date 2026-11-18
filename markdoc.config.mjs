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
  },
});
