/* Mono theme: the small script behind the header and the search dialog.

   Plain JavaScript, no dependencies, no inline handlers. It builds DOM nodes and never
   writes HTML strings. Material's own script still runs the search; this file only adds
   to it, and each part works when the other parts are missing.

   Parts: header hairline on scroll, the key hint in the search pill, the search dialog
   (Cmd/Ctrl+K, the "Try" list, section labels, one-line teasers, focus), badge tones,
   the release roadmap and the scroll reveal. */
(function () {
  "use strict";

  /* The section label of a search result: the tab of the page. The first folder of the
     address gives it; a folder that is not listed here shows DEFAULT_SECTION. A page that is
     in another tab than its folder (guides/authentication/ is in Operate) comes from the
     "sections" map that overrides/partials/search.html builds from the nav.
     Keep this object valid JSON, and change mono_sections in overrides/main.html with it. */
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

  function sectionOf(href, base, pages) {
    var path;
    try {
      path = new URL(href, location.href).pathname;
    } catch (err) {
      return DEFAULT_SECTION;
    }
    if (path.indexOf(base) === 0) path = path.slice(base.length);
    path = path.replace(/^\/+/, "");
    if (pages && Object.prototype.hasOwnProperty.call(pages, path)) return String(pages[path]);
    var first = path.split("/")[0];
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
    var pageSections = data.sections && typeof data.sections === "object" ? data.sections : null;
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
          article.insertBefore(el("span", "mono-search__section", sectionOf(link.href, base, pageSections)), article.firstChild);
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

  // ---------------------------------------------------------------------------- roadmap
  /* A release roadmap (.mono-roadmap, see mono-blocks.css). The page holds each release as an
     <article> in the <section> of its lane. This part makes one button for each release, the
     filters and the links to the release before and after, and shows one release at a time
     below its lane. The address keeps the open release (#v0.12), so a link can open one. */
  function setupRoadmap() {
    Array.prototype.forEach.call(doc.querySelectorAll(".mono-roadmap:not(.is-live)"), buildRoadmap);
  }

  function buildRoadmap(map) {
    var releases = [];
    var areas = [];
    var counts = {};
    var current = -1;
    var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    Array.prototype.forEach.call(map.querySelectorAll(".mono-roadmap__phase"), function (phase) {
      var track = el("div", "mono-roadmap__track");
      var title = phase.querySelector(".mono-roadmap__phase-name");
      track.setAttribute("role", "group");
      if (title) track.setAttribute("aria-label", title.textContent.trim());
      var first = null;
      Array.prototype.forEach.call(phase.children, function (article) {
        if (article.tagName !== "ARTICLE" || !article.id) return;
        var head = article.querySelector(".mono-roadmap__head");
        if (!head) return;
        first = first || article;

        var strong = head.querySelector("strong");
        var em = head.querySelector("em");
        var version = strong ? strong.textContent.trim() : article.id;
        var name = "";
        Array.prototype.forEach.call(head.childNodes, function (node) {
          if (node.nodeType === 3) name += node.textContent;
        });
        name = name.replace(/\s+/g, " ").trim();
        var planned = article.getAttribute("data-status") === "planned";
        var tags = (article.getAttribute("data-area") || "").split(",").map(function (tag) {
          return tag.trim();
        }).filter(Boolean);
        tags.forEach(function (tag) {
          if (!Object.prototype.hasOwnProperty.call(counts, tag)) {
            counts[tag] = 0;
            areas.push(tag);
          }
          counts[tag] += 1;
        });

        var stop = el("button", "mono-roadmap__stop" + (planned ? " is-planned" : ""));
        stop.type = "button";
        stop.setAttribute("aria-controls", article.id);
        var ver = el("span", "mono-roadmap__ver", version);
        if (em) ver.appendChild(el("small", "", em.textContent.trim()));
        stop.appendChild(ver);
        stop.appendChild(el("span", "mono-roadmap__label", name));
        track.appendChild(stop);

        var state = planned ? "planned" : "available";
        head.appendChild(el("span", "mapi-badge " + (planned ? "mapi-badge--soon" : "mapi-badge--ok"), state));
        article.setAttribute("aria-label", version + " " + name);
        releases.push({ article: article, stop: stop, title: version + " " + name, state: state, planned: planned, tags: tags });
      });
      if (first) phase.insertBefore(track, first);
    });
    if (!releases.length) return;

    var status = el("p", "mono-roadmap__status");
    status.setAttribute("role", "status");
    map.appendChild(status);

    function indexOfId(id) {
      for (var i = 0; i < releases.length; i += 1) {
        if (releases[i].article.id === id) return i;
      }
      return -1;
    }

    function fromAddress() {
      var id = location.hash.slice(1);
      try {
        id = decodeURIComponent(id);
      } catch (err) {
        /* keep the raw text */
      }
      return id ? indexOfId(id) : -1;
    }

    /* Show one release. "before" and "after" are the two nodes that must stay at the same
       place on the screen (the button that the person used), because the release above them
       can close and move the page. Without them the call comes from the address. */
    function select(index, before, after) {
      if (index < 0 || index >= releases.length) return;
      var top = before ? before.getBoundingClientRect().top : 0;
      releases.forEach(function (release, i) {
        var on = i === index;
        release.article.classList.toggle("is-on", on);
        release.article.classList.toggle("is-fresh", on && !!before && i !== current);
        if (on) release.stop.setAttribute("aria-current", "true");
        else release.stop.removeAttribute("aria-current");
      });
      current = index;
      if (!before) return;
      var moved = (after || before).getBoundingClientRect().top - top;
      if (moved) window.scrollBy({ top: moved, behavior: "instant" });
      var release = releases[index];
      status.textContent = release.title + ", " + release.state;
      try {
        history.replaceState(null, "", "#" + encodeURIComponent(release.article.id));
      } catch (err) {
        /* a page from a file: the address stays */
      }
    }

    function show(node) {
      if (node.scrollIntoView) node.scrollIntoView({ block: "nearest", behavior: still ? "auto" : "smooth" });
    }

    releases.forEach(function (release, index) {
      release.stop.addEventListener("click", function () {
        select(index, release.stop);
        show(release.article);
      });
      // The arrow keys go from stop to stop; Enter or Space opens the stop.
      release.stop.addEventListener("keydown", function (event) {
        var to = -1;
        if (event.key === "ArrowRight" || event.key === "ArrowDown") to = index + 1;
        else if (event.key === "ArrowLeft" || event.key === "ArrowUp") to = index - 1;
        else if (event.key === "Home") to = 0;
        else if (event.key === "End") to = releases.length - 1;
        if (to < 0 || to >= releases.length || event.altKey || event.ctrlKey || event.metaKey) return;
        event.preventDefault();
        releases[to].stop.focus();
      });

      var nav = el("div", "mono-roadmap__nav");
      [-1, 1].forEach(function (step) {
        var other = releases[index + step];
        if (!other) return;
        var kind = step < 0 ? "prev" : "next";
        var button = el("button", "mono-roadmap__step mono-roadmap__step--" + kind, other.title);
        button.type = "button";
        button.setAttribute("aria-label", (step < 0 ? "The release before: " : "The release after: ") + other.title);
        button.addEventListener("click", function () {
          select(index + step, release.article, other.article);
          var next = other.article.querySelector(".mono-roadmap__step--" + kind) || other.article.querySelector(".mono-roadmap__step");
          if (next) next.focus({ preventScroll: true });
          show(other.stop);
        });
        nav.appendChild(button);
      });
      release.article.appendChild(nav);
    });

    // The filters: one for each name in data-area, in the sequence of the page.
    if (areas.length > 1) {
      var filters = el("div", "mono-roadmap__filters");
      filters.setAttribute("role", "group");
      filters.setAttribute("aria-label", "Show the releases for one part");
      var buttons = [];
      var addFilter = function (label, count, tag) {
        var button = el("button", "mono-roadmap__filter", label);
        button.type = "button";
        button.appendChild(el("small", "", String(count)));
        button.setAttribute("aria-pressed", tag ? "false" : "true");
        button.addEventListener("click", function () {
          buttons.forEach(function (item) {
            item.setAttribute("aria-pressed", item === button ? "true" : "false");
          });
          releases.forEach(function (release) {
            release.stop.classList.toggle("is-dim", !!tag && release.tags.indexOf(tag) < 0);
          });
        });
        buttons.push(button);
        filters.appendChild(button);
      };
      addFilter("All", releases.length, "");
      areas.forEach(function (tag) {
        addFilter(tag, counts[tag], tag);
      });
      map.insertBefore(filters, map.firstChild);
    }

    map.classList.add("is-live");

    // Start at the release in the address, or at the newest release that is available.
    var start = fromAddress();
    var linked = start >= 0;
    if (!linked) {
      for (var i = releases.length - 1; i >= 0 && start < 0; i -= 1) {
        if (!releases[i].planned) start = i;
      }
    }
    select(Math.max(start, 0));
    if (linked && releases[start].stop.scrollIntoView) releases[start].stop.scrollIntoView({ block: "center" });

    window.addEventListener("hashchange", function () {
      var index = fromAddress();
      if (index < 0 || !doc.contains(map)) return;
      select(index);
      if (releases[index].stop.scrollIntoView) releases[index].stop.scrollIntoView({ block: "center" });
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
      setupRoadmap();
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
