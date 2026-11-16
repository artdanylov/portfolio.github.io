
if (window.innerWidth < 768) {
	[].slice.call(document.querySelectorAll('[data-bss-disabled-mobile]')).forEach(function (elem) {
		elem.classList.remove('animated');
		elem.removeAttribute('data-bss-hover-animate');
		elem.removeAttribute('data-aos');
		elem.removeAttribute('data-bss-parallax-bg');
		elem.removeAttribute('data-bss-scroll-zoom');
	});
}

document.addEventListener('DOMContentLoaded', function() {
	if ('AOS' in window) {
		AOS.init();
	}

	var hoverAnimationTriggerList = [].slice.call(document.querySelectorAll('[data-bss-hover-animate="bounce"], [data-bss-hover-animate="flash"], [data-bss-hover-animate="pulse"], [data-bss-hover-animate="rubberBand"], [data-bss-hover-animate="shake"], [data-bss-hover-animate="swing"], [data-bss-hover-animate="tada"], [data-bss-hover-animate="wobble"], [data-bss-hover-animate="jello"]'));
	var hoverAnimationList = hoverAnimationTriggerList.forEach(function (hoverAnimationEl) {
		hoverAnimationEl.addEventListener('mouseenter', function(e){ e.target.classList.add('animated', e.target.dataset.bssHoverAnimate) });
		hoverAnimationEl.addEventListener('mouseleave', function(e){ e.target.classList.remove('animated', e.target.dataset.bssHoverAnimate) });
	});

	const marquees = document.querySelectorAll('.marquee-wrapper');

	for (let marquee of marquees) {
		const track = marquee.querySelector(".marquee-track");
		const speed = marquee.dataset.marqueeSpeed || 60;
		const direction = marquee.dataset.marqueeDirection || 'left';
		const pauseOnHover = marquee.dataset.marqueePause === 'true';

		let paused = false;
		let pos = 0, lastTime = null, contentWidth = 0;
		let lastSetupWidth = 0;
		let isShrinkToFit = false;

		const observer = new ResizeObserver(setupMarquee);
		let observedElement = null;

		for (const item of Array.from(track.children)) {
			item.dataset.marqueeOriginal = '';
		}

		function setupMarquee(entries) {
			if (entries && entries.length > 0) {
				const newWidth = entries[0].contentRect.width;
				if (Math.abs(newWidth - lastSetupWidth) < 1) return;
			}

			Array.from(track.children)
				.filter(el => !('marqueeOriginal' in el.dataset))
				.forEach(el => el.remove());

			const originalItems = Array.from(track.children);
			const originalContentWidth = track.scrollWidth;

			// Measure the marquee AFTER clones are removed so we see the true CSS width.
			// When it equals the content width the element is shrink-to-fit (no explicit width,
			// e.g. inside a rotated container). Use the viewport width so enough clones are
			// added to fill the visible area, and remember the state for observer selection.
			// A marquee that is hidden (closed modal, inactive tab) has no width and isn't shrink-to-fit.
			const marqueeWidth = marquee.offsetWidth;
			isShrinkToFit = originalContentWidth > 0 && marqueeWidth <= originalContentWidth;
			const containerWidth = isShrinkToFit ? window.innerWidth : marqueeWidth;

			if (originalContentWidth > 0) {
				const copies = Math.ceil(containerWidth / originalContentWidth) + 1;
				for (let i = 0; i < copies; i++) {
					for (const item of originalItems) {
						const clone = item.cloneNode(true);
						clone.removeAttribute('data-marquee-original');
						track.appendChild(clone);
					}
				}
			}

			contentWidth = originalContentWidth;
			// Keep the current offset when setting up again, wrapped into the new content width,
			// so the marquee doesn't jump back to the start
			pos = contentWidth > 0 ? pos % contentWidth : 0;
			if (direction === "right" && pos >= 0) pos -= contentWidth;
			lastTime = null;
			// Record the width reference that the observer will report so the guard
			// matches. For shrink-to-fit we watch the document root (viewport width);
			// for explicit-width we watch the marquee itself.
			lastSetupWidth = isShrinkToFit
				? document.documentElement.clientWidth
				: marquee.offsetWidth;

			// For shrink-to-fit marquees (no explicit width), observing the marquee itself
			// creates a feedback loop: adding clones grows the wrapper, which fires the
			// observer, which adds more clones. Instead, observe the document root so only
			// genuine viewport resizes trigger re-setup. A hidden marquee is observed
			// directly, so it is set up again once it is shown.
			const elementToObserve = isShrinkToFit ? document.documentElement : marquee;

			if (elementToObserve !== observedElement) {
				if (observedElement) observer.unobserve(observedElement);
				observer.observe(elementToObserve);
				observedElement = elementToObserve;
			}
		}

		function tick(ts) {
			if (!lastTime) lastTime = ts;
			const dt = (ts - lastTime) / 1000;
			lastTime = ts;

			if (!paused && contentWidth > 0) {
				pos += (direction === "left" ? -1 : 1) * speed * dt;
				if (direction === "left" && pos <= -contentWidth) pos += contentWidth;
				if (direction === "right" && pos >= 0) pos -= contentWidth;
				track.style.transform = "translateX(" + pos + "px)";
			}

			requestAnimationFrame(tick);
		}

		if (pauseOnHover) {
			marquee.addEventListener("mouseenter", () => { paused = true; });
			marquee.addEventListener("mouseleave", () => { paused = false; });
		}

		setupMarquee();
		requestAnimationFrame(tick);

		// Images and web fonts may still be loading, which changes the content width
		if (document.readyState !== "complete") {
			window.addEventListener("load", () => setupMarquee());
		}
	}
}, false);