import React, { useState } from 'react';
import { createPortal } from 'react-dom';
import { toYoutubeEmbed, toVimeoEmbed } from '../utils/embed';

interface MediaValue {
  type?: 'image' | 'video';
  src?: string;
  caption?: string;
  width?: string;
}

interface MediaNodeViewProps {
  value: MediaValue;
  onChange: (value: MediaValue) => void;
  onRemove: () => void;
  isSelected: boolean;
}

function resolvePreviewUrl(src: string): string {
  const normalized = src.trim().replace(/\\/g, '/');
  const lower = normalized.toLowerCase();
  let clean = normalized;
  for (const marker of ['media_local/converted/', 'media_local/', 'converted/']) {
    const idx = lower.indexOf(marker);
    if (idx !== -1) {
      clean = normalized.slice(idx + marker.length);
      break;
    }
  }

  if (clean.startsWith('http://') || clean.startsWith('https://') || clean.startsWith('/')) {
    return clean;
  }
  clean = clean.replace(/^\.?\//, '');
  return `/media_local/${clean}`;
}

export function KeystaticMediaPreview({ value, onChange, onRemove, isSelected }: MediaNodeViewProps) {
  const src = (value?.src || '').trim();
  const caption = value?.caption || '';
  const width = (value?.width || '1680').trim();
  const type: 'image' | 'video' = value?.type === 'video' ? 'video' : 'image';

  const [isOpen, setIsOpen] = useState(!src);
  const [draftType, setDraftType] = useState<'image' | 'video'>(type);
  const [draftSrc, setDraftSrc] = useState(value?.src || '');
  const [draftCaption, setDraftCaption] = useState(caption);
  const [draftWidth, setDraftWidth] = useState(width);

  const openModal = () => {
    setDraftType(type);
    setDraftSrc(value?.src || '');
    setDraftCaption(caption);
    setDraftWidth(width);
    setIsOpen(true);
  };

  const saveModal = async () => {
    let finalSrc = draftSrc.trim();
    if (
      finalSrc &&
      !finalSrc.startsWith('http://') &&
      !finalSrc.startsWith('https://') &&
      !finalSrc.startsWith('/')
    ) {
      try {
        const res = await fetch(`/media_local/_resolve?name=${encodeURIComponent(finalSrc)}`);
        if (res.ok) {
          const data = await res.json();
          if (data?.path) {
            finalSrc = data.path;
          }
        }
      } catch {
        // Keep user input if resolve fails
      }
    }
    onChange({
      ...value,
      type: draftType,
      src: finalSrc,
      caption: draftCaption,
      width: draftWidth.trim() || '1680',
    });
    setIsOpen(false);
  };

  const lower = src.toLowerCase();
  const isYoutube = lower.includes('youtube.com') || lower.includes('youtu.be');
  const isVimeo = lower.includes('vimeo.com');
  const isVideo =
    !isYoutube &&
    !isVimeo &&
    (type === 'video' ||
      /\.(mp4|webm|mov|m4v)(\?.*)?$/.test(lower) ||
      lower.includes('/videos/') ||
      lower.startsWith('videos/'));

  const btnStyle: React.CSSProperties = {
    padding: '4px 10px',
    fontSize: '12px',
    borderRadius: '6px',
    border: '1px solid rgba(128,128,128,0.35)',
    background: 'rgba(128,128,128,0.1)',
    color: 'inherit',
    cursor: 'pointer',
  };

  return (
    <div
      contentEditable={false}
      style={{
        border: isSelected ? '1px solid #60a5fa' : '1px solid rgba(128,128,128,0.28)',
        borderRadius: '10px',
        padding: '10px',
        margin: '8px 0',
        display: 'flex',
        flexDirection: 'column',
        gap: '10px',
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '8px' }}>
        <span style={{ fontSize: '12px', fontWeight: 600, opacity: 0.75, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {isYoutube ? 'YouTube' : isVimeo ? 'Vimeo' : isVideo ? 'Video' : 'Image'}
          {` (${width}px)`}
          {src ? ` · ${src}` : ' (empty)'}
        </span>
        <div style={{ display: 'flex', gap: '6px', flexShrink: 0 }}>
          <button type="button" style={btnStyle} onClick={openModal}>
            Edit
          </button>
          <button
            type="button"
            style={{ ...btnStyle, color: '#f87171', borderColor: 'rgba(248,113,113,0.4)' }}
            onClick={onRemove}
            title="Delete block"
          >
            ✕ Delete
          </button>
        </div>
      </div>

      {src && (
        <div>
          {isYoutube ? (
            <iframe
              src={toYoutubeEmbed(src)}
              title={caption || 'YouTube preview'}
              style={{ width: '100%', aspectRatio: '16 / 9', border: 0, borderRadius: '8px' }}
              allowFullScreen
            />
          ) : isVimeo ? (
            <iframe
              src={toVimeoEmbed(src)}
              title={caption || 'Vimeo preview'}
              style={{ width: '100%', aspectRatio: '16 / 9', border: 0, borderRadius: '8px' }}
              allowFullScreen
            />
          ) : isVideo ? (
            <video
              src={resolvePreviewUrl(src)}
              controls
              muted
              preload="metadata"
              style={{ width: '100%', maxHeight: '360px', borderRadius: '8px', background: '#000' }}
            />
          ) : (
            <img
              src={resolvePreviewUrl(src)}
              alt={caption}
              style={{ maxWidth: '100%', maxHeight: '360px', borderRadius: '8px', objectFit: 'contain' }}
            />
          )}
        </div>
      )}

      {isOpen &&
        typeof document !== 'undefined' &&
        createPortal(
          <div
            onKeyDown={(e) => {
              if (e.key === 'Escape') setIsOpen(false);
            }}
            style={{
              position: 'fixed',
              inset: 0,
              zIndex: 9999,
              background: 'rgba(0, 0, 0, 0.65)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              padding: '16px',
            }}
          >
            <div
              onClick={(e) => e.stopPropagation()}
              style={{
                width: '100%',
                maxWidth: '520px',
                background: '#18181b',
                color: '#f4f4f5',
                border: '1px solid rgba(255,255,255,0.14)',
                borderRadius: '12px',
                padding: '20px',
                boxShadow: '0 20px 50px rgba(0,0,0,0.6)',
                display: 'flex',
                flexDirection: 'column',
                gap: '14px',
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <strong style={{ fontSize: '15px' }}>Edit Media</strong>
                <button
                  type="button"
                  onClick={() => setIsOpen(false)}
                  style={{ background: 'transparent', border: 0, color: '#a1a1aa', fontSize: '18px', cursor: 'pointer' }}
                >
                  ✕
                </button>
              </div>

              <div style={{ display: 'flex', gap: '18px', fontSize: '13px' }}>
                <label style={{ display: 'flex', alignItems: 'center', gap: '6px', cursor: 'pointer' }}>
                  <input
                    type="radio"
                    name="ks-media-type"
                    checked={draftType === 'image'}
                    onChange={() => setDraftType('image')}
                  />
                  Image (media_local/images)
                </label>
                <label style={{ display: 'flex', alignItems: 'center', gap: '6px', cursor: 'pointer' }}>
                  <input
                    type="radio"
                    name="ks-media-type"
                    checked={draftType === 'video'}
                    onChange={() => setDraftType('video')}
                  />
                  Video (media_local/videos)
                </label>
              </div>

              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                <label style={{ fontSize: '12px', color: '#a1a1aa' }}>File Name, Path or URL</label>
                <input
                  type="text"
                  autoFocus
                  placeholder="pic.webp, clip.mp4, media_local path, or URL (YouTube / Vimeo / https://...)"
                  value={draftSrc}
                  onChange={(e) => setDraftSrc(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') saveModal();
                  }}
                  style={{
                    padding: '8px 12px',
                    borderRadius: '8px',
                    border: '1px solid rgba(255,255,255,0.2)',
                    background: '#09090b',
                    color: '#fff',
                    fontSize: '14px',
                  }}
                />
              </div>

              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                <label style={{ fontSize: '12px', color: '#a1a1aa' }}>Caption / Alt Text (optional)</label>
                <input
                  type="text"
                  placeholder="Caption or alt text"
                  value={draftCaption}
                  onChange={(e) => setDraftCaption(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') saveModal();
                  }}
                  style={{
                    padding: '8px 12px',
                    borderRadius: '8px',
                    border: '1px solid rgba(255,255,255,0.2)',
                    background: '#09090b',
                    color: '#fff',
                    fontSize: '14px',
                  }}
                />
              </div>

              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                <label style={{ fontSize: '12px', color: '#a1a1aa' }}>Max Width (px)</label>
                <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                  {(['820', '1200', '1680'] as const).map((preset) => (
                    <button
                      key={preset}
                      type="button"
                      onClick={() => setDraftWidth(preset)}
                      style={{
                        ...btnStyle,
                        background: draftWidth === preset ? '#2563eb' : 'rgba(255,255,255,0.06)',
                        borderColor: draftWidth === preset ? '#2563eb' : 'rgba(255,255,255,0.18)',
                        color: '#fff',
                      }}
                    >
                      {preset}px
                    </button>
                  ))}
                  <input
                    type="text"
                    placeholder="1680"
                    value={draftWidth}
                    onChange={(e) => setDraftWidth(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') saveModal();
                    }}
                    style={{
                      width: '84px',
                      padding: '5px 10px',
                      borderRadius: '6px',
                      border: '1px solid rgba(255,255,255,0.2)',
                      background: '#09090b',
                      color: '#fff',
                      fontSize: '13px',
                    }}
                  />
                </div>
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', marginTop: '4px' }}>
                <button type="button" style={btnStyle} onClick={() => setIsOpen(false)}>
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={saveModal}
                  style={{
                    padding: '6px 14px',
                    fontSize: '13px',
                    fontWeight: 600,
                    borderRadius: '6px',
                    border: 0,
                    background: '#2563eb',
                    color: '#fff',
                    cursor: 'pointer',
                  }}
                >
                  Save
                </button>
              </div>
            </div>
          </div>,
          document.body
        )}
    </div>
  );
}
