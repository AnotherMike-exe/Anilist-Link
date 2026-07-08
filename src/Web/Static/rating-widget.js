/**
 * Shared AniList rating control — renders the right input for the user's
 * configured scoreFormat (POINT_100 / POINT_10 / POINT_10_DECIMAL / POINT_5 /
 * POINT_3) and reports the raw value AniList expects (no normalization).
 *
 * Used by the dashboard "Rate Your Completed Shows" card (via the shared
 * media detail modal) and the standalone Glance embed page.
 */
(function () {
    'use strict';

    var STAR_COUNT = 5;
    var SMILEY_LABELS = ['Bad', 'Okay', 'Great'];

    function rangeControl(containerEl, opts, min, max, step, decimals) {
        var value = opts.initialScore || min;
        containerEl.innerHTML =
            '<input type="range" class="rating-slider" min="' + min + '" max="' + max +
            '" step="' + step + '" value="' + value + '">' +
            '<span class="rating-readout">' + value.toFixed(decimals) + '</span>' +
            '<button type="button" class="btn btn-secondary btn-xs rating-submit">Submit</button>';

        var slider = containerEl.querySelector('.rating-slider');
        var readout = containerEl.querySelector('.rating-readout');
        var submitBtn = containerEl.querySelector('.rating-submit');

        slider.addEventListener('input', function () {
            readout.textContent = parseFloat(slider.value).toFixed(decimals);
        });
        submitBtn.addEventListener('click', function () {
            opts.onSubmit(parseFloat(slider.value));
        });
    }

    function starControl(containerEl, opts) {
        var value = opts.initialScore || 0;
        var starsHtml = '';
        for (var i = 1; i <= STAR_COUNT; i++) {
            starsHtml += '<span class="rating-star" data-value="' + i + '">' +
                (i <= value ? '★' : '☆') + '</span>';
        }
        containerEl.innerHTML = starsHtml +
            '<button type="button" class="btn btn-secondary btn-xs rating-submit">Submit</button>';

        var stars = containerEl.querySelectorAll('.rating-star');
        stars.forEach(function (star) {
            star.addEventListener('click', function () {
                value = parseInt(star.getAttribute('data-value'), 10);
                stars.forEach(function (s) {
                    var v = parseInt(s.getAttribute('data-value'), 10);
                    s.textContent = v <= value ? '★' : '☆';
                });
            });
        });
        containerEl.querySelector('.rating-submit').addEventListener('click', function () {
            opts.onSubmit(value);
        });
    }

    function smileyControl(containerEl, opts) {
        var value = opts.initialScore || 0;
        var glyphs = ['☹', '\u{1F610}', '\u{1F60A}'];
        var smileyHtml = '';
        for (var i = 0; i < glyphs.length; i++) {
            smileyHtml += '<span class="rating-smiley' + (value === i + 1 ? ' rating-smiley-active' : '') +
                '" data-value="' + (i + 1) + '" title="' + SMILEY_LABELS[i] + '">' + glyphs[i] + '</span>';
        }
        containerEl.innerHTML = smileyHtml +
            '<button type="button" class="btn btn-secondary btn-xs rating-submit">Submit</button>';

        var smileys = containerEl.querySelectorAll('.rating-smiley');
        smileys.forEach(function (smiley) {
            smiley.addEventListener('click', function () {
                value = parseInt(smiley.getAttribute('data-value'), 10);
                smileys.forEach(function (s) {
                    s.classList.toggle('rating-smiley-active', s === smiley);
                });
            });
        });
        containerEl.querySelector('.rating-submit').addEventListener('click', function () {
            opts.onSubmit(value);
        });
    }

    /**
     * @param {HTMLElement} containerEl
     * @param {Object} opts - { scoreFormat, initialScore, onSubmit(score) }
     */
    window.renderRatingControl = function (containerEl, opts) {
        opts = opts || {};
        opts.onSubmit = opts.onSubmit || function () {};
        containerEl.classList.add('rating-control');

        switch (opts.scoreFormat) {
            case 'POINT_100':
                rangeControl(containerEl, opts, 0, 100, 1, 0);
                break;
            case 'POINT_10_DECIMAL':
                rangeControl(containerEl, opts, 0, 10, 0.5, 1);
                break;
            case 'POINT_5':
                starControl(containerEl, opts);
                break;
            case 'POINT_3':
                smileyControl(containerEl, opts);
                break;
            case 'POINT_10':
            default:
                rangeControl(containerEl, opts, 0, 10, 1, 0);
                break;
        }
    };
})();
