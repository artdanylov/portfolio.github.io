(function () {
  var modalEl = document.getElementById('videoModal');
  if (!modalEl) return;

  var frame = document.getElementById('videoFrame');
  var label = document.getElementById('videoModalLabel');
  var sub = document.getElementById('videoModalSub');
  var desc = document.getElementById('videoModalDesc');

  modalEl.addEventListener('show.bs.modal', function (event) {
    var trigger = event.relatedTarget;
    if (!trigger) return;

    var videoId = trigger.getAttribute('data-video');
    var title = trigger.getAttribute('data-title');
    var subtitle = trigger.getAttribute('data-sub');
    var card = trigger.closest('.pf-card');

    if (!subtitle && card) {
      var subEl = card.querySelector('.pf-sub');
      if (subEl) subtitle = subEl.textContent;
    }

    if (title && label) label.textContent = title;
    if (sub) sub.textContent = subtitle ? subtitle.trim() : '';

    var descText = trigger.getAttribute('data-desc');
    if (desc) {
      desc.textContent = descText ? descText.trim() : '';
      desc.hidden = !descText;
    }

    if (videoId) {
      frame.src = 'https://www.youtube-nocookie.com/embed/' + videoId +
        '?autoplay=1&rel=0&modestbranding=1&playsinline=1';
    }
  });

  modalEl.addEventListener('hidden.bs.modal', function () {
    frame.src = '';
  });
})();

/* Page background video: expose the fixed navbar height as --nav-h,
   so page content can be kept clear of the bar. */

(function () {
  var navbar = document.querySelector('.navbar.fixed-top');
  if (!navbar) return;

  function syncNavHeight() {
    document.documentElement.style.setProperty('--nav-h', navbar.offsetHeight + 'px');
  }

  syncNavHeight();
  window.addEventListener('resize', syncNavHeight);
  window.addEventListener('load', syncNavHeight);

  if ('ResizeObserver' in window) {
    new ResizeObserver(syncNavHeight).observe(navbar);
  }
})();

/* Home page: scrolling down fades the hero text and buttons out and
   clears the dark scrim off the footage. Scrolling back up restores
   both, so the page reads the same way it did on entry. */

(function () {
  var body = document.body;
  if (!body.classList.contains('has-page-video')) return;

  var heroCopy = document.querySelector('.hero-section > .container-fluid');
  var scrim = document.querySelector('.page-video-scrim');
  if (!heroCopy && !scrim) return;

  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var ticking = false;

  // Overlay range comes from CSS, so tuning happens in one place
  var overlayStart = 1;
  var overlayEnd = 0.25;

  function readOverlayRange() {
    var styles = window.getComputedStyle(body);
    var start = parseFloat(styles.getPropertyValue('--video-overlay-start'));
    var end = parseFloat(styles.getPropertyValue('--video-overlay-end'));

    overlayStart = isNaN(start) ? 1 : start;
    overlayEnd = isNaN(end) ? overlayStart * 0.25 : end;
  }

  readOverlayRange();

  function fade(el, value) {
    if (el) el.style.opacity = String(value);
  }

  function render() {
    ticking = false;

    var y = window.scrollY || window.pageYOffset || 0;
    // Fade completes inside the scroll range that actually exists,
    // so short pages still respond and long ones don't crawl.
    var maxScroll = Math.max(
      0,
      document.documentElement.scrollHeight - window.innerHeight
    );
    var reach = Math.max(1, Math.min(window.innerHeight * 0.35, maxScroll || window.innerHeight * 0.35));
    var p = Math.min(1, Math.max(0, y / reach));
    var eased = p * p * (3 - 2 * p); // smoothstep, no linear snap
    var rest = 1 - eased;

    // Hero copy: gone, and drifting up so it doesn't feel stuck
    fade(heroCopy, rest);
    if (heroCopy && !reduced) {
      heroCopy.style.transform = 'translate3d(0, ' + (-56 * eased).toFixed(1) + 'px, 0)';
    }

    // Overlay: from --video-overlay-start down to --video-overlay-end
    if (scrim) {
      scrim.style.opacity = String(overlayStart + (overlayEnd - overlayStart) * eased);
    }

    body.classList.toggle('is-video-clear', p > 0.98);
  }

  function onScroll() {
    if (ticking) return;
    ticking = true;
    window.requestAnimationFrame(render);
  }

  render();
  window.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('resize', function () {
    readOverlayRange();
    onScroll();
  });
})();

/* Home page: "Reel" and the hero button don't navigate away — they scroll
   just far enough for the copy to clear, leaving the footage on its own.
   Same-page links are intercepted, other destinations are left untouched. */

(function () {
  var body = document.body;
  if (!body.classList.contains('has-page-video')) return;

  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // How far to travel so the fade completes (matches the render() maths)
  function clearDistance() {
    var maxScroll = Math.max(
      0,
      document.documentElement.scrollHeight - window.innerHeight
    );
    var reach = Math.min(
      window.innerHeight * 0.35,
      maxScroll || window.innerHeight * 0.35
    );

    return Math.min(maxScroll, Math.round(reach * 1.15));
  }

  function scrollToVideo() {
    var top = clearDistance();
    if (top < 8) return; // nothing to scroll, stay put

    window.scrollTo({
      top: top,
      behavior: reduced ? 'auto' : 'smooth'
    });
  }

  function isSamePage(href) {
    if (!href) return false;

    var value = href.split('#')[0];

    return (
      value === '' ||
      value === '/' ||
      value === '/index.html' ||
      value === 'index.html' ||
      value === './index.html' ||
      value === '#reel' ||
      value === '/index.html#reel'
    );
  }

  // Arriving from another page via /index.html#reel: play the same scroll
  // instead of landing at the top. Runs a few times because the browser
  // restores its own scroll position asynchronously on load, and that can
  // otherwise cancel the scroll we just started.
  function handleReelHash() {
    if (window.location.hash !== '#reel') return;

    if ('scrollRestoration' in window.history) {
      window.history.scrollRestoration = 'manual';
    }

    // Drop the hash so pressing Reel again re-triggers the scroll
    if (window.history.replaceState) {
      window.history.replaceState(null, '', window.location.pathname);
    }

    [0, 120, 400].forEach(function (delay) {
      window.setTimeout(scrollToVideo, delay);
    });
  }

  document.addEventListener('click', function (event) {
    var link = event.target.closest('a');
    if (!link) return;

    var href = link.getAttribute('href');
    var isReel = link.classList.contains('nav-link') && isSamePage(href);
    var isHeroCta = link.classList.contains('btn') && !!link.closest('.hero-section');

    if (!isReel && !isHeroCta) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;

    // Never hijack links that point somewhere else
    if (!isReel && isHeroCta && href && !isSamePage(href) && href.charAt(0) !== '#') {
      return;
    }

    event.preventDefault();
    scrollToVideo();
  });

  handleReelHash();
  window.addEventListener('load', handleReelHash);
})();

/* Contact form: sends through Web3Forms. The typed Topic always becomes
   the start of the subject line — "Contact Form Mail: <Topic>" — so every
   enquiry lands in the inbox with the same recognisable prefix. */

(function () {
  var form = document.getElementById('contactForm');
  if (!form) return;

  var ACCESS_KEY = 'e6bbe42b-84ec-4652-8e7d-5cf01ab403dd';
  var ENDPOINT = 'https://api.web3forms.com/submit';

  var topic = form.querySelector('#topic');
  var status = document.getElementById('contactStatus');
  var submit = form.querySelector('[type="submit"]');
  var sending = false;

  function say(text, state) {
    if (!status) return;

    status.textContent = text;
    status.classList.remove('d-none', 'text-primary', 'text-danger', 'text-body-tertiary');
    status.classList.add(
      state === 'ok' ? 'text-primary' : state === 'error' ? 'text-danger' : 'text-body-tertiary'
    );
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();

    if (sending) return;

    if (!form.checkValidity()) {
      form.classList.add('was-validated');
      say('Please fill in the required fields.', 'error');
      return;
    }

    var name = (form.querySelector('#name') || {}).value || '';
    var email = (form.querySelector('#email') || {}).value || '';
    var message = (form.querySelector('#message') || {}).value || '';
    var subjectText = (topic && topic.value ? topic.value : '').trim() || 'General inquiry';
    var subject = 'Contact Form Mail: ' + subjectText;

    var payload = {
      access_key: ACCESS_KEY,
      subject: subject,
      from_name: name.trim() || 'Website visitor',
      name: name.trim(),
      email: email.trim(),
      topic: subjectText,
      message: message.trim()
    };

    sending = true;
    if (submit) submit.disabled = true;
    say('Sending…', 'pending');

    fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(payload)
    })
      .then(function (response) {
        return response.json().catch(function () {
          return { success: response.ok };
        });
      })
      .then(function (data) {
        if (!data.success) throw new Error(data.message || 'Send failed');

        form.reset();
        form.classList.remove('was-validated');
        say('Thanks — your message was sent.', 'ok');
      })
      .catch(function () {
        say('Something went wrong. Please try again.', 'error');
      })
      .then(function () {
        sending = false;
        if (submit) submit.disabled = false;
      });
  });
})();

/* Footer sound toggle. Uses the #SoundToggle button from the markup and
   only activates when a background video exists, so on pages without one
   the button is hidden rather than left doing nothing. The clip is a plain
   <video> on a direct file URL, so sound switches instantly — no player
   reload and no restart. */

(function () {
  var video = document.getElementById('bgVideo') ||
    document.querySelector('.page-video-bg video');

  var button = document.getElementById('SoundToggle') ||
    document.getElementById('soundToggle');

  if (!button) return;

  // No background video on this page: make sure the button is not shown
  if (!video) {
    button.hidden = true;
    return;
  }

  var label = button.querySelector('.sound-toggle-label');
  var soundOn = false;

  button.setAttribute('aria-pressed', 'false');
  button.setAttribute('aria-label', 'Toggle background video sound');
  button.removeAttribute('hidden');

  // Theme styles do the work: outlined while muted, filled while audible
  function render(on) {
    soundOn = on;
    button.setAttribute('aria-pressed', on ? 'true' : 'false');
    button.classList.toggle('btn-primary', on);
    button.classList.toggle('btn-outline-primary', !on);

    var text = on ? 'Sound On' : 'Sound Off';

    if (label) {
      label.textContent = text;
    } else {
      button.textContent = text;
    }
  }

  function applySound(on) {
    video.muted = !on;
    video.volume = on ? 1 : 0;

    // Unmuting inside the click keeps the user activation, so the browser
    // allows the sound. Also make sure it is actually playing.
    if (on && video.paused) {
      var playing = video.play();
      if (playing && typeof playing.catch === 'function') {
        playing.catch(function () {});
      }
    }

    render(on);
  }

  button.addEventListener('click', function () {
    applySound(!soundOn);
  });

  // Start silent, and remember the state if the clip is reloaded
  video.muted = true;
  render(false);
})();