/* Mono theme: the small script behind the header and the search dialog.

   Plain JavaScript, no dependencies, no inline handlers. It builds DOM nodes and never
   writes HTML strings. Material's own script still runs the search; this file only adds
   to it, and each part works when the other parts are missing.

   Parts: header hairline on scroll, the key hint in the search pill, the search dialog
   (Cmd/Ctrl+K, the "Try" list, section labels, one-line teasers, focus), badge tones and
   the scroll reveal. */
(function () {
  "use strict";

  /* The section label of a search result, from the first folder of its address.
     A folder that is not listed here shows DEFAULT_SECTION. Keep this object valid JSON. */
  var SECTION_LABELS = {"getting-started": "Learn", "tutorials": "Learn", "concepts": "Learn", "guides": "Guides", "extend": "Extend", "reference": "Reference", "models": "Reference", "operations": "Operate"};
  var DEFAULT_SECTION = "Home";

  var doc = document;
  var root = doc.documentElement;

  function ready(fn) {
    if (doc.readyState !== "loading") fn();
    else doc.addEventListener("DOMContentLoaded", fn);
  }

  function el(tag, className, text) {
    var node = doc.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function readJson(id) {
    var node = doc.getElementById(id);
    if (!node) return null;
    try {
      return JSON.parse(node.textContent);
    } catch (err) {
      return null;
    }
  }

  /* The site root as an absolute path, from the config that Material embeds. */
  function basePath() {
    var config = readJson("__config");
    try {
      return new URL((config && config.base) || ".", location.href).pathname.replace(/\/?$/, "/");
    } catch (err) {
      return "/";
    }
  }

  function sectionOf(href, base) {
    var path;
    try {
      path = new URL(href, location.href).pathname;
    } catch (err) {
      return DEFAULT_SECTION;
    }
    if (path.indexOf(base) === 0) path = path.slice(base.length);
    var first = path.replace(/^\/+/, "").split("/")[0];
    return Object.prototype.hasOwnProperty.call(SECTION_LABELS, first) ? SECTION_LABELS[first] : DEFAULT_SECTION;
  }

  // ---------------------------------------------------------------------------- header
  function setupHeader() {
    var header = doc.querySelector(".mono-header");
    if (header) {
      var onScroll = function () {
        header.classList.toggle("is-scrolled", window.scrollY > 8);
      };
      onScroll();
      window.addEventListener("scroll", onScroll, { passive: true });
    }

    // The key hint: the Command symbol on Apple devices, "Ctrl K" elsewhere.
    var platform = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || navigator.userAgent || "";
    if (/Mac|iPhone|iPad|iPod/i.test(platform)) {
      Array.prototype.forEach.call(doc.querySelectorAll("[data-mono-kbd]"), function (kbd) {
        kbd.textContent = "⌘K";
      });
    }

    // A <label role="button"> opens with Enter through Material; add the Space key.
    doc.addEventListener("keydown", function (event) {
      var target = event.target;
      if ((event.key === " " || event.key === "Spacebar") && target instanceof HTMLLabelElement && target.getAttribute("role") === "button") {
        event.preventDefault();
        target.click();
      }
    });
  }

  // ---------------------------------------------------------------------------- search
  function setupSearch() {
    var toggle = doc.getElementById("__search");
    var dialog = doc.querySelector(".mono-search");
    if (!toggle || !dialog) return;
    var input = dialog.querySelector('[data-md-component="search-query"]');
    var result = dialog.querySelector('[data-md-component="search-result"]');
    var list = result && result.querySelector(".md-search-result__list");
    var tryBox = dialog.querySelector("[data-mono-search-try]");
    if (!input) return;

    var data = readJson("__mono_search") || {};
    var popular = Array.isArray(data.popular) ? data.popular : [];
    var base = basePath();
    var lastFocus = null;
    var wasOpen = false;

    // The "Try" list, built once.
    if (tryBox && popular.length) {
      tryBox.appendChild(el("p", "mono-search__label", "Try"));
      var ul = el("ul");
      popular.forEach(function (entry) {
        if (!entry || !entry.title || !entry.url) return;
        var link = el("a");
        link.href = entry.url;
        link.tabIndex = -1;
        link.appendChild(el("b", "", entry.title));
        if (entry.hint) link.appendChild(el("i", "", entry.hint));
        var item = el("li");
        item.appendChild(link);
        ul.appendChild(item);
      });
      tryBox.appendChild(ul);
    }

    function tryLinks() {
      return tryBox && !tryBox.hidden ? Array.prototype.slice.call(tryBox.querySelectorAll("a")) : [];
    }

    function refresh() {
      var open = toggle.checked;
      var empty = !input.value.trim();
      var showTry = open && empty && popular.length > 0;
      dialog.classList.toggle("is-empty", empty && popular.length > 0);
      if (tryBox) tryBox.hidden = !showTry;
    }

    function sync() {
      var open = toggle.checked;
      // Out of the tab order and hidden from assistive technology while closed.
      input.tabIndex = open ? 0 : -1;
      if (open) dialog.removeAttribute("aria-hidden");
      else dialog.setAttribute("aria-hidden", "true");
      refresh();
      if (open && !wasOpen) {
        if (doc.activeElement !== input) input.focus();
        // Focus again after the browser applied the open state (slow devices, iOS).
        setTimeout(function () {
          if (toggle.checked && !dialog.contains(doc.activeElement)) input.focus();
        }, 60);
      }
      if (!open && wasOpen) {
        if (dialog.contains(doc.activeElement) && doc.activeElement.blur) doc.activeElement.blur();
        if (lastFocus && doc.contains(lastFocus) && lastFocus.focus) {
          try {
            lastFocus.focus({ preventScroll: true });
          } catch (err) {
            lastFocus.focus();
          }
        }
      }
      wasOpen = open;
    }

    // Remember what had focus before the dialog took it, to give it back on close.
    doc.addEventListener("focusin", function (event) {
      if (!dialog.contains(event.target) && event.target !== doc.body) lastFocus = event.target;
    });

    toggle.addEventListener("change", sync);
    input.addEventListener("input", refresh);
    input.addEventListener("focus", function () {
      // Material opens the dialog when the input takes focus ("/" does that).
      setTimeout(sync, 0);
    });
    if (input.form) {
      input.form.addEventListener("reset", function () {
        setTimeout(refresh, 0);
      });
      // Enter in the box must never submit the form and reload the page.
      input.form.addEventListener("submit", function (event) {
        event.preventDefault();
      });
    }

    function setOpen(open) {
      if (toggle.checked === open) return;
      toggle.checked = open;
      toggle.dispatchEvent(new Event("change", { bubbles: true }));
    }

    // Capture phase on window: this runs before Material's own key handling.
    window.addEventListener("keydown", function (event) {
      var key = event.key;
      if ((key === "k" || key === "K") && (event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey) {
        event.preventDefault();
        if (toggle.checked) {
          setOpen(false);
        } else {
          setOpen(true);
          input.select();
        }
        return;
      }
      if (!toggle.checked) return;
      // Arrow keys in the "Try" list. Material does not know these links.
      if (key === "ArrowDown" || key === "ArrowUp") {
        var links = tryLinks();
        if (!links.length) return;
        var stops = [input].concat(links);
        var at = stops.indexOf(doc.activeElement);
        var next = (Math.max(at, 0) + stops.length + (key === "ArrowUp" ? -1 : 1)) % stops.length;
        stops[next].focus();
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    }, true);

    // Each result: the section label above the title and one line of text below it.
    function decorate(item) {
      var links = item.querySelectorAll(".md-search-result__link");
      Array.prototype.forEach.call(links, function (link, index) {
        var article = link.querySelector(".md-search-result__article");
        if (!article || article.hasAttribute("data-mono")) return;
        article.setAttribute("data-mono", "");
        var title = article.querySelector("h1, h2");
        if (!title) return;

        // The text after the title becomes one teaser line; the match marks stay.
        var teaser = el("p", "mono-search__teaser");
        var node = title.nextSibling;
        while (node) {
          var following = node.nextSibling;
          var keep = node.nodeType === 1 && (node.classList.contains("md-search-result__terms") || node.classList.contains("md-tags"));
          if (!keep) {
            if (node.nodeType === 3) {
              teaser.appendChild(node);
            } else if (node.nodeType === 1) {
              flatten(node, teaser);
              article.removeChild(node);
            }
          }
          node = following;
        }
        if (teaser.textContent.trim()) article.insertBefore(teaser, title.nextSibling);

        if (index === 0) {
          article.insertBefore(el("span", "mono-search__section", sectionOf(link.href, base)), article.firstChild);
        }
      });
    }

    /* Copy the text of a rich result into one line: text and <mark> only. */
    function flatten(source, target) {
      Array.prototype.forEach.call(source.childNodes, function (child) {
        if (child.nodeType === 3) {
          target.appendChild(doc.createTextNode(child.textContent));
        } else if (child.nodeType === 1) {
          if (child.tagName === "MARK") {
            var mark = el("mark", "", child.textContent);
            target.appendChild(mark);
          } else {
            flatten(child, target);
          }
        }
      });
      target.appendChild(doc.createTextNode(" "));
    }

    if (list && "MutationObserver" in window) {
      new MutationObserver(function (records) {
        records.forEach(function (record) {
          Array.prototype.forEach.call(record.addedNodes, function (node) {
            if (node.nodeType === 1 && node.classList.contains("md-search-result__item")) decorate(node);
          });
        });
      }).observe(list, { childList: true });
      Array.prototype.forEach.call(list.children, decorate);
    }

    wasOpen = toggle.checked;
    sync();
  }

  // ---------------------------------------------------------------------------- badges
  /* A version badge is neutral. "new" takes the orange tone and "available" the green. */
  function setupBadges() {
    Array.prototype.forEach.call(doc.querySelectorAll(".mapi-badge"), function (badge) {
      var text = (badge.textContent || "").trim().toLowerCase();
      if (text === "new") badge.classList.add("mapi-badge--new");
      else if (text === "available") badge.classList.add("mapi-badge--ok");
    });
  }

  // ---------------------------------------------------------------------------- reveal
  function setupReveal() {
    var nodes = Array.prototype.slice.call(doc.querySelectorAll("[data-reveal]:not(.is-in)"));
    if (!nodes.length) return;
    var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (still || !("IntersectionObserver" in window)) return;

    // Blocks that are on screen now do not move. That also means no flash on load.
    var limit = window.innerHeight || root.clientHeight;
    nodes.forEach(function (node) {
      var box = node.getBoundingClientRect();
      if (box.top < limit && box.bottom > 0) node.classList.add("is-in", "is-still");
    });
    // Siblings that enter together follow each other.
    var seen = new Map();
    nodes.forEach(function (node) {
      var count = seen.get(node.parentNode) || 0;
      if (!node.style.getPropertyValue("--i")) node.style.setProperty("--i", String(Math.min(count, 6)));
      seen.set(node.parentNode, count + 1);
    });
    root.classList.add("mono-reveal");

    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-in");
          observer.unobserve(entry.target);
        }
      });
    }, { rootMargin: "0px 0px -12% 0px", threshold: 0.05 });
    nodes.forEach(function (node) {
      if (!node.classList.contains("is-in")) observer.observe(node);
    });
  }

  // ------------------------------------------------------------------------------ start
  ready(function () {
    setupHeader();
    setupSearch();
    var page = function () {
      setupBadges();
      setupReveal();
    };
    page();
    // With instant navigation Material swaps the page and emits document$ again.
    var stream = window.document$;
    if (stream && typeof stream.subscribe === "function") {
      var first = true;
      stream.subscribe(function () {
        if (first) {
          first = false;
          return;
        }
        page();
      });
    }
  });
})();
